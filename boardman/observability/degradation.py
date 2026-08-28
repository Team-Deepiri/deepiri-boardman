"""One place to decide how a swallowed exception gets logged.

Boardman degrades gracefully almost everywhere: a Plaky lookup that fails should cost a
person's display name, not the whole reply. The cheap way to write that is
``except Exception: pass``, and the cost of the cheap way is that a genuine bug — a typo
in an attribute name, a contract change in a dependency — looks exactly like a flaky
network call and is never seen (Sorge review, PR #88).

So the handler stays broad, because the degradation still has to happen, but the *logging*
splits by failure mode:

* an **expected** failure (transport, timeout, malformed payload) is the reason the
  handler exists — one debug line, no traceback, no noise in production logs;
* anything **else** is unexpected and gets a full stack trace plus the caller's context
  string, so it shows up in the logs the first time it happens. Only the FIRST occurrence
  per call site and exception type is logged at ERROR; the rest repeat at DEBUG, so an
  ERROR count is a count of distinct faults rather than of occurrences.

Usage -- inside an ``except`` block, the exception is picked up automatically::

    except Exception:  # noqa: BLE001 - degrade, but leave a traceback
        log_degraded(_log, f"person names for board {board_id}")
        return {}

...or pass it explicitly when the handler already binds it::

    except Exception as exc:  # noqa: BLE001
        log_degraded(_log, "board schema", exc)

When the handler already writes its own contextual line, use `log_unexpected` instead --
it stays silent for the expected failures the site already reported and adds a traceback
only for the ones nobody planned for::

    except Exception as exc:  # noqa: BLE001
        _log.warning("qa fit scoring unavailable for %s: %s", repo, exc)
        log_unexpected(_log, f"qa fit scoring for {repo}", exc)
        return []
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
from types import FrameType

import httpx

# Failure modes these handlers are *for*: talking to a service that is not answering, or
# reading bytes it did not send properly. Deliberately narrow -- KeyError, TypeError,
# AttributeError and bare ValueError are NOT here, because "a typo in an attribute name"
# is the exact case this module exists to surface. Widening this list quietly turns the
# traceback back off.
_BASE_EXPECTED: tuple[type[BaseException], ...] = (
    httpx.HTTPError,  # covers RequestError, HTTPStatusError, timeouts, transport errors
    asyncio.TimeoutError,
    TimeoutError,
    OSError,  # ConnectionError, socket/DNS/file errors
    json.JSONDecodeError,  # a service answered with something that is not JSON
    UnicodeDecodeError,
)


def _optional_client_errors() -> tuple[type[BaseException], ...]:
    """Transport errors from clients whose exceptions do not inherit OSError.

    redis-py and SQLAlchemy both root their hierarchies at plain Exception, so a server
    that is simply down would otherwise read as a bug and print a stack trace on every
    call. Only the *transport* branches are listed: the base classes also cover
    ProgrammingError, WRONGTYPE and friends, which are real bugs and must keep their
    traceback.
    """
    # One lookup per name: sharing a `try` means a single rename upstream silently drops
    # the siblings, and a plain outage would start reading as a bug again.
    #
    # redis ConnectionError/TimeoutError are RedisError subclasses, unrelated to the
    # builtins of the same name (BusyLoadingError subclasses ConnectionError).
    # OutOfMemoryError/ReadOnlyError are ResponseError subclasses describing the server's
    # own state -- maxmemory reached, a write sent to a replica. Bare ResponseError
    # (WRONGTYPE, a key-namespace collision) is ours, and keeps its traceback.
    #
    # SQLAlchemy: the connection branch only. DataError/ProgrammingError/InvalidRequestError
    # are our bugs and must stay loud.
    wanted = (
        ("redis.exceptions", "ConnectionError"),
        ("redis.exceptions", "TimeoutError"),
        ("redis.exceptions", "OutOfMemoryError"),
        ("redis.exceptions", "ReadOnlyError"),
        # NOT OperationalError: this project runs SQLite, where "no such table" and
        # "no such column" -- a model change shipped without its migration -- land there
        # rather than on ProgrammingError. A dropped connection tracing once per call site
        # is a much smaller price than a missing migration degrading in silence.
        # The LLM provider SDK is a network client too: ollama.ResponseError carries
        # "model not found" and ordinary upstream HTTP errors, which agent/service.py
        # already classifies as routine (model_missing / upstream_http).
        ("ollama", "ResponseError"),
        ("sqlalchemy.exc", "InterfaceError"),
        ("sqlalchemy.exc", "DisconnectionError"),
        # NOT sqlalchemy.exc.TimeoutError either: that is connection-pool exhaustion,
        # which means sessions are being leaked. Same reasoning as OperationalError above.
    )
    extra: list[type[BaseException]] = []
    for module_name, attr in wanted:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - EVERY boardman entry point imports this module
            # Not just ImportError. A third-party package can raise anything at import
            # time (a pydantic version mismatch, a half-written wheel), and an exception
            # escaping here would stop the whole app from starting over a log-level
            # classification. Losing one entry costs a traceback nobody wanted.
            logging.getLogger(__name__).debug(
                "%s unavailable; its errors will log as unexpected", module_name, exc_info=True
            )
            continue
        exc_type = getattr(module, attr, None)
        if isinstance(exc_type, type) and issubclass(exc_type, BaseException):
            extra.append(exc_type)
        else:
            logging.getLogger(__name__).debug(
                "%s.%s is not an exception type; outages from it will log as unexpected",
                module_name,
                attr,
            )
    return tuple(extra)


EXPECTED_DEGRADATIONS: tuple[type[BaseException], ...] = _BASE_EXPECTED + _optional_client_errors()


def register_expected_degradation(exc_type: type[BaseException]) -> None:
    """Declare one of Boardman's own exception types a routine, expected state.

    Call it from the module that DEFINES the type. This module deliberately imports no
    boardman code -- everything imports it -- so the dependency has to point this way.
    """
    global EXPECTED_DEGRADATIONS
    if not (isinstance(exc_type, type) and issubclass(exc_type, BaseException)):
        raise TypeError(f"{exc_type!r} is not an exception type")
    if exc_type not in EXPECTED_DEGRADATIONS:
        EXPECTED_DEGRADATIONS = EXPECTED_DEGRADATIONS + (exc_type,)


def is_expected_degradation(exc: BaseException) -> bool:
    """True when `exc` is a failure mode the graceful-degradation path was written for."""
    return isinstance(exc, EXPECTED_DEGRADATIONS)


# One ERROR-with-traceback per (call site, exception type) per process. Many of these
# handlers sit inside per-repo or per-issue fan-outs, where one systematic fault would
# otherwise write the same stack trace dozens of times per request -- and some faults are
# permanent (a malformed AGENT_REDIS_URL fails on every cache read for the life of the
# process). Every later occurrence is still logged, but at DEBUG.
#
# THIS MEANS ERROR-LEVEL COUNTS ARE NOT OCCURRENCE COUNTS. An alert on "how many times did
# this fail" must read the DEBUG stream (the "failed again" lines, which carry the caller's
# own per-item context) or the metric counters, not the ERROR log.
#
# Keyed on the call site rather than the context string, because several callers build
# that string per item ("code search: reading owner/repo:path"): keying on it would print
# a traceback per item and grow the set without bound. A different exception type at the
# same site is a different fault and still earns its own ERROR and trace.
_traced: set[tuple[str, str, str]] = set()
# Backstop only. Repeating a traceback after a very long, very varied run is a far smaller
# problem than a set that grows for the life of the process.
_TRACED_LIMIT = 512


def reset_traced_for_tests() -> None:
    """Forget which tracebacks have already been printed. Tests only."""
    _traced.clear()


def _call_site() -> str:
    """``file:line`` of the handler that called us, skipping this module's own frames."""
    frame: FrameType | None = sys._getframe(1)
    while frame is not None and frame.f_globals.get("__name__") == __name__:
        frame = frame.f_back
    if frame is None:  # pragma: no cover - only reachable if called from this module
        return "?"
    return f"{frame.f_code.co_filename}:{frame.f_lineno}"


def _emit_unexpected(log: logging.Logger, context: str, exc: BaseException) -> None:
    key = (log.name, _call_site(), type(exc).__name__)
    if key in _traced:
        # DEBUG, not ERROR: some of these faults are permanent (a malformed AGENT_REDIS_URL
        # fails on every cache read for the life of the process). The first occurrence is
        # already an ERROR with the full traceback -- that is the actionable signal, and
        # repeating it thousands of times buries it instead of reinforcing it.
        log.debug("%s failed again with %s: %s", context, type(exc).__name__, exc)
        return
    if len(_traced) >= _TRACED_LIMIT:
        _traced.clear()
    _traced.add(key)
    # exc_info=exc, not log.exception(): the latter renders sys.exc_info(), which is
    # the wrong exception (or none at all) whenever `exc` was passed in explicitly.
    log.error("%s failed with an unexpected %s", context, type(exc).__name__, exc_info=exc)


def log_degraded(log: logging.Logger, context: str, exc: BaseException | None = None) -> None:
    """Record a swallowed exception at a level that matches how surprising it is.

    `context` should say what was being attempted, in the caller's own words -- it is the
    only clue the debug line carries about where the failure came from. `exc` defaults to
    the exception currently being handled, so a bare ``except Exception:`` needs no
    binding added just to log.
    """
    if exc is None:
        exc = sys.exc_info()[1]
    if exc is None:  # called outside an except block: nothing to classify
        log.debug("%s degraded (no active exception)", context)
        return
    if is_expected_degradation(exc):
        log.debug("%s degraded: %s: %s", context, type(exc).__name__, exc)
    else:
        _emit_unexpected(log, context, exc)


def log_unexpected(log: logging.Logger, context: str, exc: BaseException | None = None) -> None:
    """Traceback for a swallowed exception, but only when it was not an expected one.

    For handlers that already log their own line: those cover the expected failures, so
    this adds nothing for a timed-out HTTP call and a full stack trace for a TypeError.
    """
    if exc is None:
        exc = sys.exc_info()[1]
    if exc is None or is_expected_degradation(exc):
        return
    _emit_unexpected(log, context, exc)
