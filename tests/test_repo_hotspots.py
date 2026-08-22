"""Code-level repo signals: largest files, test ratio, committed-artifact smells."""

from __future__ import annotations

import httpx
import pytest

from boardman.github.repo_hotspots import fetch_repo_hotspots


def _tree_response(paths_sizes: list[tuple[str, int]]) -> dict:
    return {
        "truncated": False,
        "tree": [{"path": p, "type": "blob", "size": s} for p, s in paths_sizes],
    }


def _transport(paths_sizes: list[tuple[str, int]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/deepiri-boardman"):
            return httpx.Response(200, json={"default_branch": "main"})
        if "/git/trees/" in request.url.path:
            return httpx.Response(200, json=_tree_response(paths_sizes))
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_hotspots_ranks_largest_source_and_counts_tests(monkeypatch) -> None:
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    files = [
        ("boardman/plaky/client.py", 70000),
        ("boardman/main.py", 3500),
        ("tests/test_client.py", 5000),
        ("tests/test_main.py", 4000),
        ("README.md", 900),
        ("node_modules/dep/index.js", 999999),  # vendor must be ignored
    ]
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await fetch_repo_hotspots(c, "Team-Deepiri", "deepiri-boardman")

    assert out is not None
    assert out["source_files"] == 2  # client.py + main.py (tests counted separately)
    assert out["test_files"] == 2
    assert out["test_to_source_ratio"] == 1.0
    top = out["largest_source_files"][0]
    assert top["path"] == "boardman/plaky/client.py"
    # Size only — never a fabricated line count (bytes/35 was 25%+ off real LOC).
    assert top["size_kb"] > 60
    assert "approx_lines" not in top
    assert "line" in out["size_note"].lower()
    assert all("node_modules" not in f["path"] for f in out["largest_source_files"])


@pytest.mark.asyncio
async def test_hotspots_flags_committed_secrets_and_databases(monkeypatch) -> None:
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    files = [
        (".env", 800),
        ("boardman.db", 40000),
        ("app/id_rsa", 1600),
        (".env.example", 700),  # templates are fine
        ("app/main.py", 1200),
    ]
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await fetch_repo_hotspots(c, "Team-Deepiri", "deepiri-boardman")

    assert out is not None
    flagged = {a["path"] for a in out["tracked_artifacts"]}
    assert ".env" in flagged and "boardman.db" in flagged and "app/id_rsa" in flagged
    assert ".env.example" not in flagged


@pytest.mark.asyncio
async def test_a_deployment_can_add_its_own_artifact_rule(monkeypatch) -> None:
    """The built-in list is only as complete as the last person to look at it, so a new
    sensitive file type must be addable without a code change (Sorge review, PR #88)."""
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    monkeypatch.setattr(
        bs.settings,
        "github_extra_artifact_rules",
        "id_ed25519:private SSH key tracked in git; .tfstate:terraform state with secrets",
    )
    files = [("app/id_ed25519", 400), ("infra/main.tfstate", 9000), ("app/main.py", 1200)]
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await fetch_repo_hotspots(c, "Team-Deepiri", "deepiri-boardman")

    assert out is not None
    flagged = {a["path"]: a["why"] for a in out["tracked_artifacts"]}
    assert flagged["app/id_ed25519"] == "private SSH key tracked in git"
    assert flagged["infra/main.tfstate"] == "terraform state with secrets"


def test_an_extra_rule_without_a_reason_still_detects() -> None:
    """A security check must never fail OPEN because the explanation was left out."""
    from boardman.github.repo_hotspots import (
        _DEFAULT_EXTRA_REASON,
        _parse_extra_artifact_rules,
    )

    assert _parse_extra_artifact_rules("id_ed25519;.tfstate") == (
        ("id_ed25519", _DEFAULT_EXTRA_REASON),
        (".tfstate", _DEFAULT_EXTRA_REASON),
    )
    assert _parse_extra_artifact_rules(".jks:keystore tracked in git") == (
        (".jks", "keystore tracked in git"),
    )


def test_a_comma_in_a_reason_does_not_become_a_secret_matcher() -> None:
    """Splitting on the comma turned the tail of a reason into a matcher for api_keys.py."""
    from boardman.github.repo_hotspots import _parse_extra_artifact_rules

    rules = _parse_extra_artifact_rules("id_rsa:private ssh key, keys")
    assert rules == (("id_rsa", "private ssh key, keys"),)
    assert all(" " not in marker for marker, _why in rules)


def test_a_degenerate_marker_cannot_flag_the_whole_repository() -> None:
    """A bare "." from a trailing separator matched every file with an extension."""
    from boardman.github.repo_hotspots import _parse_extra_artifact_rules

    assert _parse_extra_artifact_rules(".jks:keystore tracked in git; .") == (
        (".jks", "keystore tracked in git"),
    )
    assert _parse_extra_artifact_rules(".") == ()
    assert _parse_extra_artifact_rules("-_.") == ()


def test_a_marker_with_spaces_is_refused_rather_than_matched() -> None:
    """A fragment of somebody's sentence must never become a committed-secret rule."""
    from boardman.github.repo_hotspots import _parse_extra_artifact_rules

    assert _parse_extra_artifact_rules("id_rsa:private ssh key; sensitive material") == (
        ("id_rsa", "private ssh key"),
    )


def test_a_malformed_extra_rule_is_skipped_not_fatal(monkeypatch) -> None:
    """A typo in one env var must not take the whole scan down with it."""
    import boardman.settings as bs
    from boardman.github import repo_hotspots
    from boardman.github.repo_hotspots import (
        _BUILTIN_ARTIFACT_RULES,
        _artifact_rules,
        _parse_extra_artifact_rules,
    )

    # Pin the setting: a checkout whose .env configures extra rules must not fail this.
    monkeypatch.setattr(bs.settings, "github_extra_artifact_rules", "")
    monkeypatch.setattr(repo_hotspots, "_rules_cache", None)

    assert _parse_extra_artifact_rules("") == ()
    # Built-ins come first and match by substring; extras are suffix-only.
    assert [(m, w) for m, w, _s in _artifact_rules()[: len(_BUILTIN_ARTIFACT_RULES)]] == list(
        _BUILTIN_ARTIFACT_RULES
    )
    assert all(suffix_only is False for _m, _w, suffix_only in _artifact_rules())
    # No marker before the colon: nothing to match on, so there is no rule to apply.
    assert _parse_extra_artifact_rules(":why but no marker; .jks:keystore tracked in git") == (
        (".jks", "keystore tracked in git"),
    )
    # Built-ins are never replaced, only extended.
    assert [(m, w) for m, w, _s in _artifact_rules()[: len(_BUILTIN_ARTIFACT_RULES)]] == list(
        _BUILTIN_ARTIFACT_RULES
    )


@pytest.mark.asyncio
async def test_hotspots_returns_none_without_a_token(monkeypatch) -> None:
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "")
    async with httpx.AsyncClient(transport=_transport([])) as c:
        assert await fetch_repo_hotspots(c, "o", "r") is None


# --- Plaky list projection: a partial board must never look complete -----------------


def test_task_envelope_reports_counts_and_never_silently_truncates() -> None:
    from boardman.agent.tools.plaky_tools import _envelope

    items = [{"id": i, "title": f"task {i}", "status": "NEEDS ASSIGNED"} for i in range(100)]
    import json

    out = json.loads(_envelope({"ok": True, "message": ""}, items, limit=60))
    assert out["returned"] == 60 and out["total"] == 100 and out["truncated"] is True
    assert "60 of 100" in out["note"]
    # Must still be VALID json (the old char-slice cut mid-object).
    assert len(out["tasks"]) == 60


def test_task_projection_keeps_status_and_assignees_only() -> None:
    from boardman.agent.tools.plaky_tools import _slim_task

    slim = _slim_task(
        {
            "id": 7,
            "title": "Fix retry crash",
            "fields": [
                {"key": "status-6", "type": "STATUS", "title": "Status", "value": "In QA"},
                {
                    "key": "person-4",
                    "type": "PERSON",
                    "title": "QA Engineer Assigned",
                    "value": {"assignedUsers": [476634]},
                },
            ],
        },
        {"476634": "Sergio Vargas"},
    )
    assert slim["id"] == 7 and slim["title"] == "Fix retry crash"
    assert slim["status"] == "In QA"
    # The id is what Plaky returns; the person is what the answer has to say.
    assert slim["assignees"][0]["users"] == ["Sergio Vargas"]
    assert "fields" not in slim  # the bulk that used to blow the size budget


def test_the_rule_list_is_parsed_once_per_configuration(monkeypatch, caplog) -> None:
    """This runs per scan; re-parsing re-warned about the same typo on every one."""
    import logging

    import boardman.settings as bs
    from boardman.github import repo_hotspots

    monkeypatch.setattr(repo_hotspots, "_rules_cache", None)
    monkeypatch.setattr(bs.settings, "github_extra_artifact_rules", "no marker here")

    with caplog.at_level(logging.WARNING, logger=repo_hotspots._log.name):
        for _ in range(5):
            repo_hotspots._artifact_rules()

    assert len([r for r in caplog.records if r.name == repo_hotspots._log.name]) == 1

    # A changed setting is re-read rather than served stale.
    monkeypatch.setattr(bs.settings, "github_extra_artifact_rules", ".jks:keystore")
    assert (".jks", "keystore", True) in repo_hotspots._artifact_rules()


def test_a_short_configured_marker_is_refused() -> None:
    """ "db" as a rule would match dbutils.py and bury every real finding."""
    from boardman.github.repo_hotspots import _parse_extra_artifact_rules

    assert _parse_extra_artifact_rules("db:database file") == ()
    assert _parse_extra_artifact_rules(".db:database file") == ((".db", "database file"),)


@pytest.mark.asyncio
async def test_a_public_key_is_never_reported_as_private_key_material(monkeypatch) -> None:
    """The rule .env.example teaches ("id_ed25519") must not flag the .pub beside it."""
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    monkeypatch.setattr(
        bs.settings, "github_extra_artifact_rules", "id_ed25519:private SSH key tracked in git"
    )
    files = [("app/id_ed25519", 400), ("app/id_ed25519.pub", 120), ("app/main.py", 1200)]
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await fetch_repo_hotspots(c, "Team-Deepiri", "deepiri-boardman")

    assert out is not None
    flagged = {a["path"] for a in out["tracked_artifacts"]}
    assert "app/id_ed25519" in flagged
    assert "app/id_ed25519.pub" not in flagged, "a public key is not private key material"


@pytest.mark.asyncio
async def test_a_configured_marker_matches_the_ending_not_the_middle(monkeypatch) -> None:
    """Substring matching is fine for the reviewed built-ins, not for arbitrary config."""
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    monkeypatch.setattr(bs.settings, "github_extra_artifact_rules", ".tfstate:terraform state")
    files = [("infra/main.tfstate", 900), ("infra/tfstate_helpers.py", 400)]
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await fetch_repo_hotspots(c, "Team-Deepiri", "deepiri-boardman")

    assert out is not None
    flagged = {a["path"] for a in out["tracked_artifacts"]}
    assert flagged == {"infra/main.tfstate"}


@pytest.mark.asyncio
async def test_per_environment_env_files_are_findings_too(monkeypatch) -> None:
    """.env.local and .env.production hold the same secrets .env does."""
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    files = [
        (".env", 400),
        (".env.local", 380),
        ("deploy/.env.production", 520),
        (".env.example", 300),  # a template is not a secret
        (".env.dist", 290),  # neither is one by another conventional name
        (".env.enc", 310),  # already encrypted at rest, committed on purpose
        (".env.gpg", 305),
        (".env.age", 302),
        (".envrc", 200),  # direnv config, meant to be committed
    ]
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await fetch_repo_hotspots(c, "Team-Deepiri", "deepiri-boardman")

    assert out is not None
    flagged = {a["path"] for a in out["tracked_artifacts"]}
    assert flagged == {".env", ".env.local", "deploy/.env.production"}


@pytest.mark.asyncio
async def test_key_material_is_never_pushed_out_of_a_capped_report(monkeypatch) -> None:
    """The report is capped, so 30 committed .env files must not hide one private key."""
    import boardman.settings as bs
    from boardman.github import repo_hotspots

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    monkeypatch.setattr(bs.settings, "github_extra_artifact_rules", "")
    monkeypatch.setattr(repo_hotspots, "_rules_cache", None)

    files = [(f"svc{i}/.env", 200) for i in range(30)]
    files.append(("zz_last/id_rsa", 1600))  # sorts last by path, must still be reported
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await fetch_repo_hotspots(c, "Team-Deepiri", "deepiri-boardman")

    assert out is not None
    reported = out["tracked_artifacts"]
    assert reported[0]["path"] == "zz_last/id_rsa", "private key material comes first"
    # The per-rule quota is what actually protects it: 30 .env files take 5 slots, not 20.
    env_rows = [r for r in reported if r["path"].endswith("/.env")]
    assert len(env_rows) == repo_hotspots._MAX_PATHS_PER_RULE
    assert "of 31" in out["tracked_artifacts_note"], "truncation is stated, not silent"
    assert "per rule" in out["tracked_artifacts_note"]


@pytest.mark.asyncio
async def test_a_complete_artifact_report_says_nothing_about_truncation(monkeypatch) -> None:
    import boardman.settings as bs
    from boardman.github import repo_hotspots

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    monkeypatch.setattr(bs.settings, "github_extra_artifact_rules", "")
    monkeypatch.setattr(repo_hotspots, "_rules_cache", None)

    async with httpx.AsyncClient(transport=_transport([(".env", 100)])) as c:
        out = await fetch_repo_hotspots(c, "Team-Deepiri", "deepiri-boardman")

    assert out is not None
    assert out["tracked_artifacts_note"] == ""


def test_a_ci_or_test_env_file_is_still_a_finding() -> None:
    """.env.ci and .env.test routinely hold real CI credentials."""
    from boardman.github.repo_hotspots import _ENV_NOT_A_FINDING_SUFFIXES

    assert ".ci" not in _ENV_NOT_A_FINDING_SUFFIXES
    assert ".test" not in _ENV_NOT_A_FINDING_SUFFIXES


@pytest.mark.asyncio
async def test_a_configured_key_rule_survives_a_capped_report(monkeypatch) -> None:
    """The setting exists to add NEW key formats; env files must not cap them out. The
    per-rule quota guarantees a seat without guessing how severe the new rule is."""
    import boardman.settings as bs
    from boardman.github import repo_hotspots

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    monkeypatch.setattr(
        bs.settings, "github_extra_artifact_rules", "id_ed25519:private SSH key tracked in git"
    )
    monkeypatch.setattr(repo_hotspots, "_rules_cache", None)

    files = [(f"svc{i}/.env", 200) for i in range(25)]
    files.append(("zz/id_ed25519", 400))
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await fetch_repo_hotspots(c, "Team-Deepiri", "deepiri-boardman")

    assert out is not None
    reported = {a["path"] for a in out["tracked_artifacts"]}
    assert "zz/id_ed25519" in reported, "25 .env files must not cap out the new key rule"
    assert len([p for p in reported if p.endswith("/.env")]) == 5


@pytest.mark.asyncio
async def test_a_noisy_configured_rule_cannot_hide_key_material(monkeypatch) -> None:
    """Severity is never guessed from an operator's prose, so a reason that merely says
    "no secrets inside" cannot promote a data file over a real private key."""
    import boardman.settings as bs
    from boardman.github import repo_hotspots

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    monkeypatch.setattr(
        bs.settings, "github_extra_artifact_rules", ".parquet:data extract, no secrets inside"
    )
    monkeypatch.setattr(repo_hotspots, "_rules_cache", None)

    files = [(f"aa_data/part{i}.parquet", 900) for i in range(40)]
    files.append(("zz/server.pem", 1200))
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await fetch_repo_hotspots(c, "Team-Deepiri", "deepiri-boardman")

    assert out is not None
    reported = out["tracked_artifacts"]
    assert reported[0]["path"] == "zz/server.pem", "real key material still leads"
    assert len([r for r in reported if r["path"].endswith(".parquet")]) == (
        repo_hotspots._MAX_PATHS_PER_RULE
    )


def test_an_unconfigured_repo_has_no_routing_rather_than_an_error(monkeypatch, caplog) -> None:
    """A repo outside the org has no placement. That is config, not a failure."""
    import logging

    from boardman.agent.tools import github_tools

    monkeypatch.setattr("boardman.repos_config.get_routing", lambda *_a, **_k: None)
    with caplog.at_level(logging.DEBUG, logger=github_tools.logger.name):
        assert github_tools._repo_routing_summary("someoneelse/notours") == {}

    assert [r for r in caplog.records if r.exc_info] == [], "no stack trace for a normal state"


@pytest.mark.asyncio
async def test_a_sqlite_sidecar_gets_its_own_reason(monkeypatch) -> None:
    """The matcher breaks on first hit, so ".db" must not shadow ".db-wal"/".db-shm"."""
    import boardman.settings as bs
    from boardman.github import repo_hotspots

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    monkeypatch.setattr(bs.settings, "github_extra_artifact_rules", "")
    monkeypatch.setattr(repo_hotspots, "_rules_cache", None)

    files = [("app.db", 4000), ("app.db-wal", 900), ("app.db-shm", 300)]
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await fetch_repo_hotspots(c, "Team-Deepiri", "deepiri-boardman")

    assert out is not None
    why = {a["path"]: a["why"] for a in out["tracked_artifacts"]}
    assert why["app.db"] == "SQLite database committed to the repo"
    assert why["app.db-wal"] == "SQLite write-ahead log committed to the repo"
    assert why["app.db-shm"] == "SQLite shared-memory file committed to the repo"


@pytest.mark.asyncio
async def test_the_truncation_note_does_not_invent_key_material(monkeypatch) -> None:
    """The note is handed straight to the agent; it must not imply keys were found."""
    import boardman.settings as bs
    from boardman.github import repo_hotspots

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    monkeypatch.setattr(bs.settings, "github_extra_artifact_rules", "")
    monkeypatch.setattr(repo_hotspots, "_rules_cache", None)

    files = [(f"svc{i}/.env", 200) for i in range(25)]
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await fetch_repo_hotspots(c, "Team-Deepiri", "deepiri-boardman")

    assert out is not None
    note = out["tracked_artifacts_note"]
    assert "of 25" in note
    assert "private key" not in note, "no keys were found; the note must not say otherwise"


@pytest.mark.asyncio
async def test_an_extension_rule_matches_the_ending_not_the_middle(monkeypatch) -> None:
    """README.db.md is documentation, data.dbf is a dBase file, my.pemfile is neither."""
    import boardman.settings as bs
    from boardman.github import repo_hotspots

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    monkeypatch.setattr(bs.settings, "github_extra_artifact_rules", "")
    monkeypatch.setattr(repo_hotspots, "_rules_cache", None)

    files = [
        ("docs/README.db.md", 900),
        ("data/sample.dbf", 800),
        ("keys/my.pemfile", 700),
        ("prod.db", 40000),
        ("keys/server.pem", 1200),
        ("app/id_rsa_backup", 1600),  # a name fragment IS still substring-matched
    ]
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await fetch_repo_hotspots(c, "Team-Deepiri", "deepiri-boardman")

    assert out is not None
    flagged = {a["path"] for a in out["tracked_artifacts"]}
    assert flagged == {"prod.db", "keys/server.pem", "app/id_rsa_backup"}


@pytest.mark.asyncio
async def test_versioned_and_backed_up_artifacts_are_still_found(monkeypatch) -> None:
    """db.sqlite3 is the Django default name, and a .bak of a private key is still a key."""
    import boardman.settings as bs
    from boardman.github import repo_hotspots

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    monkeypatch.setattr(bs.settings, "github_extra_artifact_rules", "")
    monkeypatch.setattr(repo_hotspots, "_rules_cache", None)

    files = [
        ("db.sqlite3", 50000),
        ("keys/server.pem.bak", 1200),
        ("prod.db.backup", 40000),
        ("keys/store.p12~", 900),
        ("docs/notes.md", 500),
    ]
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await fetch_repo_hotspots(c, "Team-Deepiri", "deepiri-boardman")

    assert out is not None
    assert {a["path"] for a in out["tracked_artifacts"]} == {
        "db.sqlite3",
        "keys/server.pem.bak",
        "prod.db.backup",
        "keys/store.p12~",
    }


@pytest.mark.asyncio
async def test_false_positives_cannot_exhaust_a_rules_quota(monkeypatch) -> None:
    """The per-rule quota makes a sloppy match expensive: it can hide the real finding."""
    import boardman.settings as bs
    from boardman.github import repo_hotspots

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    monkeypatch.setattr(bs.settings, "github_extra_artifact_rules", "")
    monkeypatch.setattr(repo_hotspots, "_rules_cache", None)

    files = [(f"docs/aa{i}.db.md", 500) for i in range(10)]
    files.append(("zz/prod.db", 40000))
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await fetch_repo_hotspots(c, "Team-Deepiri", "deepiri-boardman")

    assert out is not None
    assert {a["path"] for a in out["tracked_artifacts"]} == {"zz/prod.db"}


@pytest.mark.parametrize(
    "path,marker",
    [
        # A wrapper does not change what the file holds.
        ("data/data.db.gz", ".db"),
        ("dumps/backup.sqlite.zip", ".sqlite"),
        ("app.db.tar.gz", ".db"),
        ("certs/keystore.p12.enc", ".p12"),
        ("certs/cert.pem.crt", ".pem"),
        # Nor does renaming it. A private key called .txt is a private key, and a .sql
        # dump of a committed database is that database.
        ("certs/key.pem.txt", ".pem"),
        ("certs/deploy_key.pem.bak.txt", ".pem"),
        ("dumps/prod.db.sql", ".db"),
        ("data/prod.db.backup", ".db"),
        ("data/db.sqlite3", ".sqlite"),
        # Every spelling of an environment file, including the commonest one.
        ("deploy/production.env", ".env"),
        ("prod.env", ".env"),
        (".env", ".env"),
        (".env.local", ".env"),
    ],
)
def test_these_are_findings(path: str, marker: str) -> None:
    """A security-detection list is only as good as what it still catches. Each of these
    was reported before the extension test existed, and a miss here costs far more than a
    line in a report."""
    from boardman.github.repo_hotspots import artifact_hit

    assert artifact_hit(path, marker) is True


@pytest.mark.parametrize(
    "path,marker",
    [
        # Prose about the thing, or code that uses it -- a different kind of file.
        ("docs/README.db.md", ".db"),
        ("src/schema.db.py", ".db"),
        ("config/config.db.json", ".db"),
        # ".db" is just letters here: no separator after it.
        ("data/data.dbf", ".db"),
        # A public key is never a finding, however it is spelled.
        ("keys/id_ed25519.pub", "id_ed25519"),
        ("certs/server.pem.pub", ".pem"),
        # Templates hold placeholders; .envrc is a direnv config committed on purpose.
        ("config/.env.example", ".env"),
        ("config/config.env.example", ".env"),
        (".envrc", ".env"),
    ],
)
def test_these_are_not(path: str, marker: str) -> None:
    from boardman.github.repo_hotspots import artifact_hit

    assert artifact_hit(path, marker) is False


def test_a_configured_marker_matches_the_file_not_every_mention_of_it() -> None:
    """An operator writing `id_ed25519:private SSH key` means the file.

    Substring-matching a configured rule reports `docs/id_ed25519_rotation.md` and
    `scripts/rotate_id_ed25519.py` as committed private keys, which buries the real
    finding under noise -- the thing the minimum-length rule and the per-rule quota are
    both there to prevent.
    """
    from boardman.github.repo_hotspots import artifact_hit

    assert artifact_hit("keys/id_ed25519", "id_ed25519", suffix_only=True) is True
    assert artifact_hit("keys/id_ed25519.bak", "id_ed25519", suffix_only=True) is True
    assert artifact_hit("docs/id_ed25519_rotation.md", "id_ed25519", suffix_only=True) is False
    assert artifact_hit("scripts/rotate_id_ed25519.py", "id_ed25519", suffix_only=True) is False

    # The built-in `id_rsa` rule is a substring rule on purpose, so id_rsa_backup counts.
    assert artifact_hit("keys/id_rsa_backup", "id_rsa") is True


def test_prose_about_a_key_is_not_the_key() -> None:
    """`id_rsa` is a substring rule on purpose, so `id_rsa_backup` and `id_rsa.old` count.

    Documentation and scripts do not. `id_rsa` sits in the top priority band with a
    five-path quota, so a handful of doc files naming it crowd real .env and .db findings
    out of a report that holds twenty rows.
    """
    from boardman.github.repo_hotspots import artifact_hit

    assert artifact_hit("keys/id_rsa", "id_rsa") is True
    assert artifact_hit("keys/id_rsa_backup", "id_rsa") is True
    assert artifact_hit("keys/id_rsa.old", "id_rsa") is True
    assert artifact_hit("docs/id_rsa_rotation.md", "id_rsa") is False
    assert artifact_hit("scripts/rotate_id_rsa.sh", "id_rsa") is False


def test_the_specific_database_markers_still_have_to_come_first() -> None:
    """`_extension_hit` accepts a `-` after the marker -- that is what keeps
    `server.pem-old` a finding -- so ".db" does match "prod.db-wal". The matcher breaks on
    the first hit, so a reorder would report a WAL file as a database, with the wrong
    reason and sharing the ".db" quota."""
    from boardman.github.repo_hotspots import _BUILTIN_ARTIFACT_RULES, artifact_hit

    assert artifact_hit("data/prod.db-wal", ".db") is True, "the overlap is real"
    order = [marker for marker, _why in _BUILTIN_ARTIFACT_RULES]
    assert order.index(".db-wal") < order.index(".db")
    assert order.index(".db-shm") < order.index(".db")
