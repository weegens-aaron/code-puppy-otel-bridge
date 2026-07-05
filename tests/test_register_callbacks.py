"""Pins for otel_bridge.register_callbacks -- the degrade-gracefully paths
and the Phase 2 baggage/session-grouping wiring.

These tests exercise ``_instrument()`` / ``_on_agent_run_context()``
directly (not through the real host callback dispatch) so they stay fast
and host-independent. Every failure branch must be a clean, non-raising
early return -- that's the entire point of this module (see its
docstring). The baggage tests use the REAL opentelemetry-api/-sdk
installed in this dev env (they're a hard dependency of the feature, so
stubbing them would pin nothing).
"""

from __future__ import annotations

import asyncio

import pytest

from otel_bridge import register_callbacks as rc


@pytest.fixture(autouse=True)
def _reset_instrumentation_state():
    """_INSTRUMENTED is a one-way module-level switch; reset it per test."""
    rc._INSTRUMENTED = False
    rc._LAST_STATUS_REASON = "not started yet"
    yield
    rc._INSTRUMENTED = False
    rc._LAST_STATUS_REASON = "not started yet"


def test_instrument_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(rc.config, "is_enabled", lambda: False)
    rc._instrument()
    assert rc._INSTRUMENTED is False
    assert "disabled" in rc._LAST_STATUS_REASON


def test_instrument_noop_when_endpoint_unset(monkeypatch):
    monkeypatch.setattr(rc.config, "is_enabled", lambda: True)
    monkeypatch.setattr(rc.config, "get_endpoint", lambda: None)
    rc._instrument()
    assert rc._INSTRUMENTED is False
    assert "endpoint is unset" in rc._LAST_STATUS_REASON


def test_instrument_noop_when_otel_sdk_missing(monkeypatch):
    monkeypatch.setattr(rc.config, "is_enabled", lambda: True)
    monkeypatch.setattr(rc.config, "get_endpoint", lambda: "http://localhost:4318/v1/traces")
    monkeypatch.setattr(rc, "_try_import_otel_sdk", lambda: None)
    rc._instrument()
    assert rc._INSTRUMENTED is False
    assert "opentelemetry-sdk" in rc._LAST_STATUS_REASON


@pytest.mark.parametrize(
    "break_stage", ["endpoint", "sdk"], ids=["endpoint-unset", "sdk-missing"]
)
def test_banner_emitted_on_enabled_but_broken(monkeypatch, break_stage):
    """Enabled-but-not-active must surface a visible banner pointing at
    /otel-setup, not just a log line."""
    banners: list[str] = []
    monkeypatch.setattr(rc, "_emit_setup_banner", banners.append)
    monkeypatch.setattr(rc.config, "is_enabled", lambda: True)
    if break_stage == "endpoint":
        monkeypatch.setattr(rc.config, "get_endpoint", lambda: None)
    else:
        monkeypatch.setattr(
            rc.config, "get_endpoint", lambda: "http://localhost:4318/v1/traces"
        )
        monkeypatch.setattr(rc, "_try_import_otel_sdk", lambda: None)
    rc._instrument()
    assert len(banners) == 1


def test_no_banner_when_disabled(monkeypatch):
    """Disabled-by-default users must never see otel_bridge noise."""
    monkeypatch.setattr(
        rc, "_emit_setup_banner", lambda reason: pytest.fail("no banner!")
    )
    monkeypatch.setattr(rc.config, "is_enabled", lambda: False)
    rc._instrument()


# ---------------------------------------------------------------------------
# SDK span-creation self-check (api/sdk version-skew guard)
# ---------------------------------------------------------------------------


def test_self_check_passes_with_real_sdk():
    from opentelemetry.sdk.trace import TracerProvider

    assert rc._sdk_self_check(TracerProvider) is None


def test_self_check_reports_broken_sdk_with_versions():
    class _ExplodingProvider:
        def get_tracer(self, name):
            raise AttributeError(
                "type object 'TraceFlags' has no attribute 'RANDOM_TRACE_ID'"
            )

    reason = rc._sdk_self_check(_ExplodingProvider)
    assert reason is not None
    assert "RANDOM_TRACE_ID" in reason
    assert "opentelemetry-api" in reason  # names the packages to align


def test_instrument_aborts_and_banners_when_self_check_fails(monkeypatch):
    """A skewed SDK must mean NO instrumentation (one broken span per
    agent run would break the interactive loop -- the 2026-07-04
    incident), plus a visible banner."""
    banners: list[str] = []
    monkeypatch.setattr(rc, "_emit_setup_banner", banners.append)
    monkeypatch.setattr(rc.config, "is_enabled", lambda: True)
    monkeypatch.setattr(
        rc.config, "get_endpoint", lambda: "http://localhost:4318/v1/traces"
    )

    class _Boom:
        def get_tracer(self, name):
            raise AttributeError("skewed")

    monkeypatch.setattr(
        rc,
        "_try_import_otel_sdk",
        lambda: (None, None, _Boom, None, None),
    )
    rc._instrument()
    assert rc._INSTRUMENTED is False
    assert "self-check" in rc._LAST_STATUS_REASON
    assert len(banners) == 1


def test_banner_swallows_messaging_failures(monkeypatch):
    """_emit_setup_banner must never raise, even if the host UI is gone."""
    import code_puppy.messaging as messaging

    def _boom(*args, **kwargs):
        raise RuntimeError("UI exploded")

    monkeypatch.setattr(messaging, "emit_warning", _boom)
    rc._emit_setup_banner("anything")  # must not raise


def test_instrument_is_idempotent(monkeypatch):
    """A second call after success must not re-run the whole probe."""
    calls = {"n": 0}

    def _fake_is_enabled():
        calls["n"] += 1
        return True

    rc._INSTRUMENTED = True
    monkeypatch.setattr(rc.config, "is_enabled", _fake_is_enabled)
    rc._instrument()
    assert calls["n"] == 0  # short-circuited before touching config at all


def test_instrument_succeeds_with_stubbed_sdk_and_pydantic_ai(monkeypatch):
    """Full happy path, with every external dependency stubbed out.

    This pins the WIRING (what gets called with what), not real OTel/
    pydantic-ai behavior -- that needs a live smoke test per AGENTS.md
    "Remaining work", since no OTel SDK is installed in this dev env.
    """
    calls: dict[str, object] = {}

    class _FakeExporter:
        def __init__(self, endpoint=None, headers=None):
            calls["exporter_endpoint"] = endpoint
            calls["exporter_headers"] = headers

    class _FakeResource:
        @staticmethod
        def create(attrs):
            calls["resource_attrs"] = attrs
            return "resource-sentinel"

    class _FakeProvider:
        def __init__(self, resource=None):
            calls["provider_resource"] = resource
            self.processors = []

        def add_span_processor(self, processor):
            self.processors.append(processor)

    class _FakeBatchProcessor:
        def __init__(self, exporter):
            calls["batch_processor_exporter"] = exporter

    def _fake_set_tracer_provider(provider):
        calls["set_tracer_provider_arg"] = provider

    monkeypatch.setattr(
        rc,
        "_try_import_otel_sdk",
        lambda: (
            _FakeExporter,
            _FakeResource,
            _FakeProvider,
            _FakeBatchProcessor,
            _fake_set_tracer_provider,
        ),
    )
    # This test pins the WIRING; the self-check is pinned separately
    # (and a fake provider can't pass the real one).
    monkeypatch.setattr(rc, "_sdk_self_check", lambda cls: None)

    class _FakeInstrumentationSettings:
        def __init__(self, tracer_provider=None):
            calls["instrumentation_settings_provider"] = tracer_provider

    class _FakeAgent:
        @staticmethod
        def instrument_all(settings):
            calls["instrument_all_settings"] = settings

    fake_pydantic_ai = type(
        "FakeModule",
        (),
        {"Agent": _FakeAgent, "InstrumentationSettings": _FakeInstrumentationSettings},
    )
    monkeypatch.setitem(
        __import__("sys").modules, "pydantic_ai", fake_pydantic_ai
    )

    monkeypatch.setattr(rc.config, "is_enabled", lambda: True)
    monkeypatch.setattr(
        rc.config, "get_endpoint", lambda: "http://localhost:3000/api/public/otel/v1/traces"
    )
    monkeypatch.setattr(rc.config, "get_headers", lambda: {"Authorization": "Basic xyz"})
    monkeypatch.setattr(rc.config, "get_service_name", lambda: "code-puppy-test")

    rc._instrument()

    assert rc._INSTRUMENTED is True
    assert "instrumented ->" in rc._LAST_STATUS_REASON
    assert calls["exporter_endpoint"] == "http://localhost:3000/api/public/otel/v1/traces"
    assert calls["exporter_headers"] == {"Authorization": "Basic xyz"}
    assert calls["resource_attrs"] == {"service.name": "code-puppy-test"}
    assert calls["instrument_all_settings"] is not None


# ---------------------------------------------------------------------------
# Phase 2: baggage predicate + agent_run_context wiring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "allowed"),
    [
        ("session.id", True),
        ("code_puppy.agent", True),
        ("code_puppy.group_id", True),
        ("user.id", False),  # we never set it; don't copy other people's
        ("langfuse.session.id", False),  # vendor key: not ours, not copied
        ("totally.unrelated", False),
        ("", False),
    ],
)
def test_baggage_key_predicate_is_a_narrow_allow_list(key, allowed):
    assert rc._baggage_key_allowed(key) is allowed


def test_session_id_is_stable_and_process_scoped():
    assert rc.SESSION_ID.startswith("code-puppy-")
    assert rc.SESSION_ID == rc.SESSION_ID  # one constant, not a factory


def test_agent_run_context_returns_none_when_not_instrumented():
    assert rc._on_agent_run_context(object(), object(), "g-1", []) is None


def test_agent_run_context_scopes_baggage_around_the_run():
    """Inside the CM the run-identifying baggage is attached; after, gone."""
    from opentelemetry import baggage

    rc._INSTRUMENTED = True

    class _FakeAgent:
        name = "rowsdower"

    cm = rc._on_agent_run_context(_FakeAgent(), object(), "group-42", [])
    assert cm is not None

    async def _exercise():
        async with cm:
            return (
                baggage.get_baggage("session.id"),
                baggage.get_baggage("code_puppy.agent"),
                baggage.get_baggage("code_puppy.group_id"),
            )

    inside = asyncio.run(_exercise())
    assert inside == (rc.SESSION_ID, "rowsdower", "group-42")
    # Context token detached on exit -- nothing leaks into the test runner.
    assert baggage.get_baggage("session.id") is None


def test_agent_run_context_omits_blank_agent_name_and_group():
    from opentelemetry import baggage

    rc._INSTRUMENTED = True

    class _NamelessAgent:
        name = ""

    cm = rc._on_agent_run_context(_NamelessAgent(), object(), None, [])

    async def _exercise():
        async with cm:
            return (
                baggage.get_baggage("session.id"),
                baggage.get_baggage("code_puppy.agent"),
                baggage.get_baggage("code_puppy.group_id"),
            )

    assert asyncio.run(_exercise()) == (rc.SESSION_ID, None, None)


def test_baggage_processor_attached_on_instrument_happy_path(monkeypatch):
    """_try_add_baggage_processor puts a real BaggageSpanProcessor on the
    provider (the package is installed in this dev env) and reports True."""

    class _FakeProvider:
        def __init__(self):
            self.processors = []

        def add_span_processor(self, processor):
            self.processors.append(processor)

    provider = _FakeProvider()
    assert rc._try_add_baggage_processor(provider) is True
    assert len(provider.processors) == 1


def test_baggage_processor_missing_degrades_gracefully(monkeypatch):
    """Simulate the package being absent: False, no raise, no processor."""
    import builtins

    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name.startswith("opentelemetry.processor.baggage"):
            raise ImportError("simulated absence")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    class _FakeProvider:
        def __init__(self):
            self.processors = []

        def add_span_processor(self, processor):
            self.processors.append(processor)

    provider = _FakeProvider()
    assert rc._try_add_baggage_processor(provider) is False
    assert provider.processors == []


# ---------------------------------------------------------------------------
# Host wiring: importing the module must register both hooks
# ---------------------------------------------------------------------------


def test_hooks_registered_with_host_on_import():
    """Importing register_callbacks (done at collection time) must have
    placed our two callbacks in code-puppy's real registry."""
    import code_puppy.callbacks as cp_callbacks

    assert rc._on_startup in cp_callbacks._callbacks["startup"]
    assert rc._on_agent_run_context in cp_callbacks._callbacks["agent_run_context"]
