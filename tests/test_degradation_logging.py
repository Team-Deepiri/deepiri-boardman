"""A swallowed exception must still be findable.

Boardman degrades gracefully in ~100 places: a Plaky lookup that fails costs a display
name, not the answer. Written as `except Exception: pass`, that also hides genuine bugs --
a typo in an attribute name looks exactly like a flaky network call and nobody ever sees
it (Sorge review, PR #88). These tests pin the split: expected failures stay quiet,
everything else leaves a full traceback.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from boardman.observability.degradation import (
    is_expected_degradation,
    log_degraded,
    log_unexpected,
    reset_traced_for_tests,
)


@pytest.fixture(autouse=True)
def _forget_printed_tracebacks():
    """The traceback dedupe is process-wide; each test starts from a clean slate."""
    reset_traced_for_tests()
    yield
    reset_traced_for_tests()


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        TimeoutError(),
        OSError("socket"),
        json.JSONDecodeError("bad", "{}", 0),
        UnicodeDecodeError("utf-8", bytes([255]), 0, 1, "invalid"),
    ],
)
def test_transport_and_decode_failures_are_the_expected_kind(exc: BaseException) -> None:
    assert is_expected_degradation(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("boom"),
        ZeroDivisionError(),
        NameError("typo"),
        # The cases the module exists for: a bug in our own code reading a payload.
        AttributeError("'dict' object has no attribute 'nmae'"),
        TypeError("argument of type 'NoneType' is not iterable"),
        KeyError("id"),
        IndexError("list index out of range"),
        ValueError("shape"),
    ],
)
def test_a_programming_error_is_never_the_expected_kind(exc: BaseException) -> None:
    """Widening the expected list is how the traceback gets quietly switched back off."""
    assert is_expected_degradation(exc) is False


def test_a_redis_outage_is_expected_even_though_it_is_not_an_oserror() -> None:
    """redis-py raises RedisError(Exception); misread as a bug it is a traceback per call."""
    redis_exceptions = pytest.importorskip("redis.exceptions")
    assert is_expected_degradation(redis_exceptions.ConnectionError("refused")) is True


def test_a_dropped_database_connection_is_expected() -> None:
    """SQLAlchemyError roots at Exception too, and the sweep/reconcile loops run per row."""
    from sqlalchemy.exc import DisconnectionError, InterfaceError

    assert is_expected_degradation(InterfaceError("connect", {}, Exception("gone"))) is True
    assert is_expected_degradation(DisconnectionError("pool recycled")) is True


def test_pool_exhaustion_is_a_leak_not_an_outage() -> None:
    """QueuePool timeout means sessions are not being returned. That is our bug."""
    from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

    assert is_expected_degradation(SQLAlchemyTimeoutError("QueuePool limit reached")) is False


def test_a_missing_migration_is_never_written_off_as_an_outage() -> None:
    """On SQLite "no such table" is an OperationalError, so that whole class stays loud."""
    from sqlalchemy.exc import OperationalError

    missing_table = OperationalError(
        "SELECT * FROM github_webhook_delivery", {}, Exception("no such table")
    )
    assert is_expected_degradation(missing_table) is False


def test_only_the_transport_branch_of_those_hierarchies_is_expected() -> None:
    """Listing the base classes would silence exactly the bugs this module exists for."""
    from sqlalchemy.exc import InvalidRequestError, ProgrammingError

    redis_exceptions = pytest.importorskip("redis.exceptions")

    # Misusing a session across tasks, or a bad query: our bug, keep the traceback.
    assert is_expected_degradation(InvalidRequestError("session already begun")) is False
    assert is_expected_degradation(ProgrammingError("SELECT nope", {}, Exception())) is False
    # WRONGTYPE from a key-namespace collision: also our bug, not an outage.
    assert is_expected_degradation(redis_exceptions.ResponseError("WRONGTYPE")) is False
    assert is_expected_degradation(redis_exceptions.DataError("bad value")) is False
    # ...while the transport branch still degrades quietly.
    assert is_expected_degradation(redis_exceptions.TimeoutError("slow")) is True
    # A server describing its own state is an outage too, not our bug: maxmemory with
    # noeviction, or a write sent to a replica.
    assert is_expected_degradation(redis_exceptions.OutOfMemoryError("maxmemory")) is True
    assert is_expected_degradation(redis_exceptions.ReadOnlyError("replica")) is True


def test_a_handled_webhook_failure_is_not_reported_as_a_crash() -> None:
    """The retry loop raises to reach its own bookkeeping; that is not an unexpected bug."""
    from boardman.jobs.handlers import _SoftSyncFailure

    assert issubclass(_SoftSyncFailure, RuntimeError)
    assert (
        is_expected_degradation(_SoftSyncFailure("plaky said no")) is False
    ), "it is still not in the expected list -- handlers.py routes it before log_degraded"


def test_expected_failure_logs_one_debug_line_and_no_traceback(caplog) -> None:
    log = logging.getLogger("boardman.test.degradation")
    with caplog.at_level(logging.DEBUG, logger=log.name):
        try:
            raise httpx.ConnectError("plaky unreachable")
        except Exception:  # noqa: BLE001 - the pattern under test
            log_degraded(log, "person names")

    (record,) = (r for r in caplog.records if r.name == log.name)
    assert record.levelno == logging.DEBUG
    assert "person names" in record.getMessage()
    assert record.exc_info is None, "a routine network blip must not print a stack trace"


def test_an_unexpected_failure_is_logged_with_its_stack_trace(caplog) -> None:
    log = logging.getLogger("boardman.test.degradation")
    with caplog.at_level(logging.DEBUG, logger=log.name):
        try:
            raise AssertionError("this is a bug, not a blip")
        except Exception:  # noqa: BLE001 - the pattern under test
            log_degraded(log, "person names")

    (record,) = (r for r in caplog.records if r.name == log.name)
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None, "the whole point is the traceback"
    assert "AssertionError" in record.getMessage()


def test_the_exception_can_be_passed_explicitly(caplog) -> None:
    """Handlers that already bind the exception should not have to re-raise to log it."""
    log = logging.getLogger("boardman.test.degradation.explicit")
    with caplog.at_level(logging.DEBUG, logger=log.name):
        log_degraded(log, "outside any handler", httpx.ConnectError("x"))

    (record,) = (r for r in caplog.records if r.name == log.name)
    assert record.levelno == logging.DEBUG


def test_an_explicit_exception_is_the_one_whose_traceback_is_printed(caplog) -> None:
    """log.exception() would render sys.exc_info() -- the wrong exception, or none."""
    log = logging.getLogger("boardman.test.degradation.explicit")
    passed = RuntimeError("the one the caller handed us")
    with caplog.at_level(logging.DEBUG, logger=log.name):
        try:
            raise ValueError("a different exception, currently being handled")
        except ValueError:
            log_degraded(log, "explicit wins", passed)

    (record,) = (r for r in caplog.records if r.name == log.name)
    assert record.exc_info is not None
    assert record.exc_info[1] is passed, "the passed exception, not the ambient one"


def test_an_explicit_exception_outside_any_handler_still_gets_a_traceback(caplog) -> None:
    log = logging.getLogger("boardman.test.degradation.explicit")
    passed = RuntimeError("nothing is being handled right now")
    with caplog.at_level(logging.DEBUG, logger=log.name):
        log_unexpected(log, "no active exception", passed)

    (record,) = (r for r in caplog.records if r.name == log.name)
    assert record.exc_info is not None and record.exc_info[1] is passed


def test_log_unexpected_stays_silent_when_the_site_already_reported_it(caplog) -> None:
    """Sites with their own warning line cover the expected failures; do not double up."""
    log = logging.getLogger("boardman.test.degradation.unexpected")
    with caplog.at_level(logging.DEBUG, logger=log.name):
        try:
            raise httpx.ReadTimeout("slow")
        except Exception:  # noqa: BLE001 - the pattern under test
            log_unexpected(log, "qa fit scoring")

    assert [r for r in caplog.records if r.name == log.name] == []


def test_log_unexpected_still_escalates_a_real_bug(caplog) -> None:
    log = logging.getLogger("boardman.test.degradation.unexpected")
    with caplog.at_level(logging.DEBUG, logger=log.name):
        try:
            raise RuntimeError("unhandled state")
        except Exception:  # noqa: BLE001 - the pattern under test
            log_unexpected(log, "qa fit scoring")

    (record,) = (r for r in caplog.records if r.name == log.name)
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None


def test_calling_it_outside_a_handler_does_not_raise(caplog) -> None:
    """Defensive: a refactor that moves the call out of the except block must not crash."""
    log = logging.getLogger("boardman.test.degradation.bare")
    with caplog.at_level(logging.DEBUG, logger=log.name):
        log_degraded(log, "nothing happening")
        log_unexpected(log, "nothing happening")
    assert len([r for r in caplog.records if r.name == log.name]) == 1


def test_an_unpulled_ollama_is_reported_as_a_fact_not_a_stack_trace(caplog, monkeypatch) -> None:
    """With the shipped defaults this runs on every chat turn; it must not flood the log."""
    from boardman.agent import service
    from boardman.llm import ollama_autodetect
    from boardman.settings import settings

    monkeypatch.setattr(settings, "llm_provider", "ollama")
    monkeypatch.setattr(settings, "llm_model", "")

    def no_models(_request_model=None):
        raise ollama_autodetect.NoOllamaModelAvailable("Ollama has no models.")

    monkeypatch.setattr(ollama_autodetect, "effective_ollama_model", no_models)

    with caplog.at_level(logging.DEBUG, logger=service.logger.name):
        label = service._default_model_for_provider("ollama")

    assert label == "auto-selected from Ollama"
    assert [r for r in caplog.records if r.exc_info] == [], "no traceback for a normal state"


def test_an_unreachable_ollama_still_gets_investigated(caplog, monkeypatch) -> None:
    """A bug in the resolver is a different thing from nothing having been pulled."""
    from boardman.agent import service
    from boardman.llm import ollama_autodetect
    from boardman.settings import settings

    monkeypatch.setattr(settings, "llm_provider", "ollama")
    monkeypatch.setattr(settings, "llm_model", "")

    def broken(_request_model=None):
        raise AttributeError("'NoneType' object has no attribute 'strip'")

    monkeypatch.setattr(ollama_autodetect, "effective_ollama_model", broken)

    with caplog.at_level(logging.DEBUG, logger=service.logger.name):
        assert service._default_model_for_provider("ollama") == "auto-selected from Ollama"

    assert [r for r in caplog.records if r.exc_info], "a real bug keeps its traceback"


def test_every_optional_client_error_actually_resolved() -> None:
    """A rename upstream must not silently drop a whole outage class from the list."""
    from boardman.observability.degradation import EXPECTED_DEGRADATIONS

    names = {f"{c.__module__}.{c.__name__}" for c in EXPECTED_DEGRADATIONS}
    for expected in (
        "redis.exceptions.ConnectionError",
        "redis.exceptions.TimeoutError",
        "redis.exceptions.OutOfMemoryError",
        "redis.exceptions.ReadOnlyError",
        "sqlalchemy.exc.InterfaceError",
        "sqlalchemy.exc.DisconnectionError",
    ):
        assert expected in names, f"{expected} dropped out of EXPECTED_DEGRADATIONS"
    assert (
        "sqlalchemy.exc.OperationalError" not in names
    ), "SQLite reports a missing table as OperationalError; it must keep its traceback"
    assert (
        "sqlalchemy.exc.TimeoutError" not in names
    ), "pool exhaustion means leaked sessions; it must keep its traceback"


def test_a_fan_out_prints_one_traceback_not_one_per_row(caplog) -> None:
    """These handlers sit in per-repo loops, and several build the context per item, so
    the dedupe keys on the call site rather than on the string it was handed."""
    log = logging.getLogger("boardman.test.degradation.fanout")
    with caplog.at_level(logging.DEBUG, logger=log.name):
        for i in range(25):
            try:
                raise AttributeError("'NoneType' object has no attribute 'get'")
            except Exception:  # noqa: BLE001 - the pattern under test
                log_degraded(log, f"fetch_repo_metadata(repo{i})")

    records = [r for r in caplog.records if r.name == log.name]
    assert len(records) == 25, "every row still reports; nothing is hidden"
    assert len([r for r in records if r.exc_info]) == 1, "one traceback for one fault"
    assert records[0].levelno == logging.ERROR, "the first occurrence is the loud one"
    assert records[-1].levelno == logging.DEBUG, "repeats must not shout on every row"
    assert "repo24" in records[-1].getMessage(), "the repeat still names its own row"
    assert "failed again" in records[-1].getMessage()


def test_a_second_different_fault_at_the_same_site_still_traces(caplog) -> None:
    """A latch that fires on the first bug of any kind hides every bug after it."""
    log = logging.getLogger("boardman.test.degradation.fanout")
    with caplog.at_level(logging.DEBUG, logger=log.name):
        for exc in (AttributeError("first bug"), TypeError("a different bug")):
            try:
                raise exc
            except Exception:  # noqa: BLE001 - the pattern under test
                log_degraded(log, "same call site, two faults")

    traced = [r for r in caplog.records if r.name == log.name and r.exc_info]
    assert [type(r.exc_info[1]).__name__ for r in traced] == ["AttributeError", "TypeError"]


def test_distinct_call_sites_each_get_their_own_traceback(caplog) -> None:
    log = logging.getLogger("boardman.test.degradation.fanout")
    with caplog.at_level(logging.DEBUG, logger=log.name):
        try:
            raise AttributeError("bug")
        except Exception:  # noqa: BLE001 - the pattern under test
            log_degraded(log, "site one")
        try:
            raise AttributeError("bug")
        except Exception:  # noqa: BLE001 - the pattern under test
            log_degraded(log, "site two")

    assert len([r for r in caplog.records if r.name == log.name and r.exc_info]) == 2


def test_the_dedupe_set_cannot_grow_without_bound() -> None:
    """A long-lived process must not accumulate a key per distinct fault forever."""
    from boardman.observability import degradation

    log = logging.getLogger("boardman.test.degradation.bounded")
    for i in range(degradation._TRACED_LIMIT + 50):
        exc_type = type(f"Synthetic{i}Error", (RuntimeError,), {})
        try:
            raise exc_type("x")
        except Exception:  # noqa: BLE001 - the pattern under test
            log_degraded(log, "bounded")

    assert len(degradation._traced) <= degradation._TRACED_LIMIT


def test_a_provider_saying_the_model_is_missing_is_not_a_bug() -> None:
    """The LLM SDK is a network client; "model not found" is its routine answer."""
    ollama = pytest.importorskip("ollama")

    assert is_expected_degradation(ollama.ResponseError("model 'x' not found")) is True


def test_boardman_can_register_its_own_expected_states() -> None:
    """degradation.py imports no boardman code, so the dependency points inward."""
    from boardman.llm.ollama_autodetect import NoOllamaModelAvailable
    from boardman.observability.degradation import register_expected_degradation

    assert is_expected_degradation(NoOllamaModelAvailable("nothing pulled")) is True
    # Registration is idempotent and refuses anything that is not an exception type.
    register_expected_degradation(NoOllamaModelAvailable)
    with pytest.raises(TypeError):
        register_expected_degradation("not a type")  # type: ignore[arg-type]


def test_a_broken_optional_package_cannot_stop_the_app_from_starting(monkeypatch) -> None:
    """Every boardman entry point imports this module, so nothing here may escape."""
    import importlib

    from boardman.observability import degradation

    def explode(name: str):
        # Not ImportError: a version mismatch inside the package raises whatever it likes.
        raise TypeError(f"{name} is built against a different pydantic")

    monkeypatch.setattr(importlib, "import_module", explode)
    assert degradation._optional_client_errors() == ()
