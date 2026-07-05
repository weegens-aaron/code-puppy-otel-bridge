"""Pins for /otel-setup: dep detection, installer selection, auth helper,
walkthrough branching, and the startup banner it pairs with.

Same philosophy as test_register_callbacks.py: exercise the functions
directly, stub the messaging emitters (they're host UI, not behavior
under test), and use the REAL installed OTel packages where they're a
hard dependency of the assertion.
"""

from __future__ import annotations

import base64
import importlib.util
import sys

import pytest

from otel_bridge import register_callbacks as rc
from otel_bridge import setup_command as sc


@pytest.fixture(autouse=True)
def _reset_instrumentation_state():
    rc._INSTRUMENTED = False
    rc._LAST_STATUS_REASON = "not started yet"
    yield
    rc._INSTRUMENTED = False
    rc._LAST_STATUS_REASON = "not started yet"


@pytest.fixture()
def emitted(monkeypatch):
    """Capture (level, text) from the host messaging emitters."""
    records: list[tuple[str, str]] = []
    import code_puppy.messaging as messaging

    for level in ("error", "info", "success", "warning"):
        monkeypatch.setattr(
            messaging,
            f"emit_{level}",
            lambda text, _level=level, **kw: records.append((_level, str(text))),
        )
    return records


# ---------------------------------------------------------------------------
# Dep detection + installer selection
# ---------------------------------------------------------------------------


def test_missing_deps_empty_in_dev_env():
    """This dev env has all three (the baggage/E2E tests rely on them)."""
    assert sc.missing_deps() == []


def test_missing_deps_detects_absent_probe(monkeypatch):
    real_find_spec = importlib.util.find_spec

    def _fake(name, *args, **kwargs):
        if name == "opentelemetry.processor.baggage":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", _fake)
    assert sc.missing_deps() == ["opentelemetry-processor-baggage"]


def test_missing_deps_survives_find_spec_raising(monkeypatch):
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **k: (_ for _ in ()).throw(ModuleNotFoundError(name)),
    )
    assert sc.missing_deps() == list(sc._DEP_NAMES)


def test_install_argv_prefers_pip(monkeypatch):
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **k: object() if name == "pip" else real_find_spec(name),
    )
    argv = sc._install_argv(["pkg-a"])
    assert argv[:4] == [sys.executable, "-m", "pip", "install"]


def test_install_argv_falls_back_to_uv(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)
    monkeypatch.setattr(sc.shutil, "which", lambda name: "C:/fake/uv.exe")
    argv = sc._install_argv(["pkg-a"])
    assert argv == ["uv", "pip", "install", "--python", sys.executable, "pkg-a"]


def test_install_argv_none_when_stuck(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)
    monkeypatch.setattr(sc.shutil, "which", lambda name: None)
    assert sc._install_argv(["pkg-a"]) is None


def test_install_deps_reports_manual_path_when_stuck(monkeypatch):
    monkeypatch.setattr(sc, "_install_argv", lambda pkgs: None)
    ok, detail = sc.install_deps(["pkg-a"])
    assert ok is False
    assert "pip install pkg-a" in detail


def test_install_deps_reports_installer_failure(monkeypatch):
    class _Result:
        returncode = 1
        stdout = ""
        stderr = "boom: resolver exploded"

    monkeypatch.setattr(sc, "_install_argv", lambda pkgs: ["fake"])
    monkeypatch.setattr(sc.subprocess, "run", lambda *a, **k: _Result())
    ok, detail = sc.install_deps(["pkg-a"])
    assert ok is False
    assert "resolver exploded" in detail


# ---------------------------------------------------------------------------
# Durability heuristics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exe", "expect_fragment"),
    [
        # uvx cached envs (THE supported launch scenario) per OS:
        (
            r"C:\Users\x\AppData\Local\uv\cache\archive-v0\abc\Scripts\python.exe",
            "uvx --with",
        ),
        ("/home/x/.cache/uv/archive-v0/abc/bin/python3", "uvx --with"),
        ("/Users/x/Library/Caches/uv/archive-v0/abc/bin/python3", "uvx --with"),
        # Off the paved path: no note, install still works.
        (r"C:\my\plain\venv\Scripts\python.exe", None),
        ("/usr/bin/python3", None),
        ("/opt/homebrew/bin/python3", None),
    ],
)
def test_durability_note_env_detection(monkeypatch, exe, expect_fragment):
    monkeypatch.setattr(sc.sys, "executable", exe)
    note = sc.durability_note()
    if expect_fragment is None:
        assert note is None
    else:
        assert expect_fragment in note


# ---------------------------------------------------------------------------
# /otel-setup auth
# ---------------------------------------------------------------------------


def test_auth_writes_basic_header(monkeypatch, emitted):
    written = {}
    monkeypatch.setattr(
        sc.config, "set_headers", lambda value: written.update(value=value)
    )
    assert sc.handle_otel_setup_command("/otel-setup auth pk-123 sk-456") is True
    expected = base64.b64encode(b"pk-123:sk-456").decode()
    assert written["value"] == f"Authorization=Basic {expected}"
    assert any(level == "success" for level, _ in emitted)
    # Security invariant: warn that typed secrets persist in the host's
    # command history file (see AGENTS.md "Security posture").
    assert any(
        level == "warning" and "command_history" in text
        for level, text in emitted
    )
    # And the secret itself never appears in any output.
    assert not any("sk-456" in text for _, text in emitted)


def test_auth_wrong_arity_is_an_error_not_a_write(monkeypatch, emitted):
    monkeypatch.setattr(
        sc.config,
        "set_headers",
        lambda value: pytest.fail("must not write on bad args"),
    )
    sc.handle_otel_setup_command("/otel-setup auth only-one")
    assert any(level == "error" for level, _ in emitted)


# ---------------------------------------------------------------------------
# Walkthrough branching
# ---------------------------------------------------------------------------


def test_walkthrough_lists_todo_when_unconfigured(monkeypatch, emitted):
    monkeypatch.setattr(sc, "missing_deps", lambda: [])
    monkeypatch.setattr(sc.config, "get_endpoint", lambda: None)
    monkeypatch.setattr(sc.config, "get_headers", lambda: {})
    monkeypatch.setattr(sc.config, "is_enabled", lambda: False)
    monkeypatch.setattr(
        rc, "_instrument", lambda: pytest.fail("must not instrument yet")
    )
    sc.handle_otel_setup_command("/otel-setup")
    text = "\n".join(t for _, t in emitted)
    assert "/set otel_bridge_endpoint" in text
    assert "/set otel_bridge_enabled true" in text


def test_walkthrough_installs_then_stops_on_failure(monkeypatch, emitted):
    monkeypatch.setattr(sc, "missing_deps", lambda: ["opentelemetry-sdk"])
    monkeypatch.setattr(sc, "install_deps", lambda pkgs: (False, "no network"))
    sc.handle_otel_setup_command("/otel-setup")
    assert any("no network" in t for level, t in emitted if level == "error")


def test_walkthrough_activates_live_when_fully_configured(monkeypatch, emitted):
    monkeypatch.setattr(sc, "missing_deps", lambda: [])
    monkeypatch.setattr(sc.config, "get_endpoint", lambda: "http://x/v1/traces")
    monkeypatch.setattr(sc.config, "get_headers", lambda: {"Authorization": "s3cret"})
    monkeypatch.setattr(sc.config, "is_enabled", lambda: True)

    def _fake_instrument():
        rc._INSTRUMENTED = True
        rc._LAST_STATUS_REASON = "instrumented -> http://x/v1/traces"

    monkeypatch.setattr(rc, "_instrument", _fake_instrument)
    sc.handle_otel_setup_command("/otel-setup")
    assert any("instrumented ->" in t for level, t in emitted if level == "success")
    # Secrets stay secret: header VALUES never appear in output.
    assert not any("s3cret" in t for _, t in emitted)


def test_walkthrough_freshly_installed_but_not_hotloadable_says_restart(
    monkeypatch, emitted
):
    """Install succeeded this process + self-check failure = advise a
    restart (honest), not the scary version-mismatch error."""
    monkeypatch.setattr(sc, "missing_deps", lambda: ["opentelemetry-sdk"])
    monkeypatch.setattr(sc, "install_deps", lambda pkgs: (True, "uv pip ..."))
    monkeypatch.setattr(sc, "durability_note", lambda: None)
    monkeypatch.setattr(sc.config, "get_endpoint", lambda: "http://x/v1/traces")
    monkeypatch.setattr(sc.config, "get_headers", lambda: {})
    monkeypatch.setattr(sc.config, "is_enabled", lambda: True)

    def _fake_instrument():
        rc._LAST_STATUS_REASON = "OTel SDK failed span-creation self-check (...)"

    monkeypatch.setattr(rc, "_instrument", _fake_instrument)
    # After "install", the probe finds nothing missing (healthy on disk).
    monkeypatch.setattr(
        sc, "missing_deps", _once_then_empty(["opentelemetry-sdk"])
    )
    sc.handle_otel_setup_command("/otel-setup")
    assert any(
        level == "warning" and "Restart code-puppy" in text
        for level, text in emitted
    )
    assert not any(level == "error" for level, _ in emitted)


def _once_then_empty(first):
    """missing_deps stub: 'first' on the first call, [] afterwards."""
    calls = {"n": 0}

    def _probe():
        calls["n"] += 1
        return first if calls["n"] == 1 else []

    return _probe


def test_walkthrough_reports_already_instrumented(monkeypatch, emitted):
    rc._INSTRUMENTED = True
    rc._LAST_STATUS_REASON = "instrumented -> http://x/v1/traces"
    monkeypatch.setattr(sc, "missing_deps", lambda: [])
    monkeypatch.setattr(sc.config, "get_endpoint", lambda: "http://x/v1/traces")
    monkeypatch.setattr(sc.config, "get_headers", lambda: {})
    monkeypatch.setattr(sc.config, "is_enabled", lambda: True)
    monkeypatch.setattr(
        rc, "_instrument", lambda: pytest.fail("idempotent path must skip")
    )
    sc.handle_otel_setup_command("/otel-setup")
    assert any(level == "success" for level, _ in emitted)


# ---------------------------------------------------------------------------
# Registration with the host
# ---------------------------------------------------------------------------


def test_otel_setup_registered_with_host():
    from code_puppy.command_line.command_registry import get_command

    cmd = get_command("otel-setup")
    assert cmd is not None
    assert cmd.handler is sc.handle_otel_setup_command
