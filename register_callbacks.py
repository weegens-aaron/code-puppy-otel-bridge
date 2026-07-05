"""otel_bridge wiring: startup instrumentation + a diagnostic slash command.

Design in one breath: pydantic-ai (code-puppy's agent runtime) already
emits OpenTelemetry GenAI semantic-convention spans natively via
``Agent.instrument_all()`` -- no core changes needed, just flip the
switch. This module does exactly that, once, on the host's ``startup``
callback (not eagerly at import time -- config isn't guaranteed loaded
yet at plugin-import time, and ``startup`` is the host's documented seam
for "do a thing once the app is up").

Everything here is defensive by design: missing optional dependencies or
missing config are logged once and skipped, never raised, so a
misconfigured or half-installed otel_bridge can never break plugin
loading or the interactive loop for anything else.

Module layout:
  * This file -- wiring only: startup hook, diagnostic command, and the
    "enabled but not active" startup banner.
  * :mod:`config` -- config key reads (own ``otel_bridge_*`` namespace).
  * :mod:`setup_command` -- ``/otel-setup``: guided dep install + config
    walkthrough (imported below so its command registers with the host).

Phase 2 (per-run session/trace grouping) rides the host's
``agent_run_context`` callback: every ``pydantic_agent.run()`` gets
wrapped in an async context manager that attaches OTel Baggage
(process-wide session id, agent name, run group id), and a
``BaggageSpanProcessor`` (verified: ``opentelemetry-processor-baggage``,
see AGENTS.md) copies those baggage entries onto every span created
during the run. Backends that map the generic ``session.id`` attribute
(Langfuse does) then group all of one code-puppy session's traces
together. The processor uses a narrow allow-list predicate -- W3C
Baggage propagates into outbound HTTP headers of any instrumented
client, so we only ever put non-sensitive identifiers in it and only
copy our own keys onto spans.

See ``AGENTS.md`` for the verified plugin-loader contract and the
verified pydantic-ai OTel API.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any

from code_puppy.callbacks import register_callback
from code_puppy.command_line.command_registry import register_command

from . import config
from . import setup_command  # noqa: F401  (registers /otel-setup on import)

logger = logging.getLogger(__name__)

__all__ = ["handle_otel_status_command"]

# One session id per code-puppy process: every agent turn (main agent,
# subagents, inspectors) in this process shares it, so a backend that
# groups traces by ``session.id`` shows the whole interactive session as
# one browsable unit. Deliberately NOT persisted across restarts -- a
# process IS the natural session boundary here.
SESSION_ID = f"code-puppy-{uuid.uuid4().hex[:12]}"

# Baggage keys this plugin owns. The BaggageSpanProcessor predicate
# below only copies these onto spans -- never arbitrary baggage that
# other libraries may have attached (least surprise, least leakage).
_BAGGAGE_SESSION_KEY = "session.id"  # generic OTel-ish key; Langfuse maps it
_BAGGAGE_PREFIX = "code_puppy."  # our own namespaced extras

# Flips true only after a fully successful instrument_all() call, so the
# status command and tests can distinguish "tried and failed" from
# "succeeded". Never reset to False -- instrumentation is a one-way,
# process-lifetime switch (mirrors pydantic-ai's own "instrument every
# agent, existing and future" semantics).
_INSTRUMENTED = False

# Populated on a successful instrument attempt so /otel-status can report
# WHY it's off without re-running the whole probe.
_LAST_STATUS_REASON = "not started yet"


def _emit_setup_banner(reason: str) -> None:
    """One visible TUI line when tracing is wanted but not working.

    Louder sibling of the logger warnings: users who enabled the bridge
    should not have to dig through logs to learn their spans went
    nowhere. Points at /otel-setup, which fixes what it can (installs
    missing deps) and spells out the rest. Fully defensive -- messaging
    being unavailable (bare pytest, host internals shifting) must never
    break instrumentation or startup.
    """
    try:
        from code_puppy.messaging import emit_warning

        emit_warning(
            f"otel_bridge: tracing enabled but NOT active -- {reason}. "
            "Run /otel-setup to fix."
        )
    except Exception:
        logger.debug("otel_bridge: setup banner emit failed", exc_info=True)


def _try_import_otel_sdk():
    """Import the optional OTel SDK + OTLP HTTP exporter pieces.

    Returns the tuple of classes/functions needed to build a
    TracerProvider, or None if any of them aren't installed. This is the
    ONE optional-dependency boundary in this plugin -- everything below
    it assumes these imports succeeded.
    """
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.trace import set_tracer_provider
    except ImportError:
        return None
    return (
        OTLPSpanExporter,
        Resource,
        TracerProvider,
        BatchSpanProcessor,
        set_tracer_provider,
    )


def _emit_notice(message: str) -> None:
    """Best-effort informational line in the TUI; logger fallback."""
    try:
        from code_puppy.messaging import emit_info

        emit_info(message)
    except Exception:
        logger.info(message)


def _auto_install_missing_deps() -> bool:
    """Install missing runtime deps at startup (owner decision 2026-07-04).

    Setting ``otel_bridge_enabled=true`` is consent to install what the
    bridge needs -- so every environment (uvx cache rebuilds, fresh
    per-project venvs) self-heals at startup instead of requiring
    per-project onboarding. Runs ONLY when enabled and an endpoint is
    configured (callers guarantee that); never raises -- a failed
    install just leaves the existing degrade-gracefully paths to report
    what's still missing. Returns True if it installed something, so
    the caller can tell "fresh install, import state may be stale"
    apart from a genuinely broken SDK.
    """
    missing = setup_command.missing_deps()
    if not missing:
        return False
    _emit_notice(
        f"otel_bridge: installing missing tracing deps "
        f"({', '.join(missing)}) -- one-time per environment..."
    )
    ok, detail = setup_command.install_deps(missing)
    if not ok:
        logger.warning(f"otel_bridge: dep auto-install failed -- {detail}")
        return False
    logger.info(f"otel_bridge: auto-installed {', '.join(missing)} via `{detail}`")
    note = setup_command.durability_note()
    if note:
        logger.info(
            f"otel_bridge: {note} (optional -- deps now auto-install "
            "at startup whenever they go missing)"
        )
    return True


def _sdk_self_check(tracer_provider_cls: Any) -> str | None:
    """Prove the OTel SDK can actually CREATE a span before we instrument.

    Imports succeeding is not enough: mismatched opentelemetry-api /
    opentelemetry-sdk versions import fine but explode at span-creation
    time (real incident 2026-07-04: sdk 1.43.0 + api 1.41.1 ->
    ``AttributeError: TraceFlags has no attribute RANDOM_TRACE_ID`` on
    EVERY agent run, breaking the interactive loop). A throwaway
    provider with no exporters exercises the same span-creation path;
    nothing leaves the process.

    Returns None when healthy, else a human-readable reason (including
    both package versions, so the fix is obvious from /otel-status).
    """
    try:
        tracer = tracer_provider_cls().get_tracer("otel_bridge.selfcheck")
        span = tracer.start_span("otel_bridge.selfcheck")
        span.end()
        return None
    except Exception as exc:
        api_version = sdk_version = "unknown"
        try:
            import opentelemetry.sdk.version
            import opentelemetry.version

            api_version = opentelemetry.version.__version__
            sdk_version = opentelemetry.sdk.version.__version__
        except Exception:  # pragma: no cover - best-effort version report
            pass
        return (
            f"OTel SDK failed span-creation self-check "
            f"({type(exc).__name__}: {exc}). Installed opentelemetry-api "
            f"{api_version} / opentelemetry-sdk {sdk_version} are likely "
            f"mismatched -- align them (e.g. uv pip install --upgrade "
            f"opentelemetry-api opentelemetry-sdk) and restart"
        )


def _baggage_key_allowed(key: str) -> bool:
    """Allow-list predicate for the BaggageSpanProcessor.

    Only the keys this plugin sets in :func:`_on_agent_run_context` get
    copied onto spans. Never widen this to ALLOW_ALL_BAGGAGE_KEYS --
    baggage can arrive from anywhere (including remote callers via W3C
    propagation) and blindly copying it onto spans is how junk and
    secrets end up in your trace backend.
    """
    return key == _BAGGAGE_SESSION_KEY or key.startswith(_BAGGAGE_PREFIX)


def _try_add_baggage_processor(provider: Any) -> bool:
    """Attach the baggage-to-span-attributes processor, if installed.

    Optional-dependency boundary number two (package:
    ``opentelemetry-processor-baggage``). Missing it degrades to "spans
    still flow, they just aren't session-grouped" -- worth one log line,
    not worth failing instrumentation.
    """
    try:
        from opentelemetry.processor.baggage import BaggageSpanProcessor
    except ImportError:
        logger.warning(
            "otel_bridge: opentelemetry-processor-baggage not installed; "
            "spans will flow but won't carry session.id grouping. "
            "Run: pip install opentelemetry-processor-baggage"
        )
        _emit_setup_banner(
            "spans will flow but session grouping is off "
            "(opentelemetry-processor-baggage missing)"
        )
        return False
    provider.add_span_processor(BaggageSpanProcessor(_baggage_key_allowed))
    return True


def _instrument() -> None:
    """Build a TracerProvider from config and switch on pydantic-ai OTel.

    Idempotent -- a second call is a no-op once ``_INSTRUMENTED`` is
    True. Every failure path logs one clear line and returns; it never
    raises (this runs from the host's ``startup`` callback dispatch,
    which logs-and-continues on exceptions anyway, but we don't rely on
    that -- a plugin that needs its host to save it from its own bugs
    isn't done yet).
    """
    global _INSTRUMENTED, _LAST_STATUS_REASON

    if _INSTRUMENTED:
        return

    if not config.is_enabled():
        _LAST_STATUS_REASON = (
            "disabled (set otel_bridge_enabled=true via /set to turn on)"
        )
        logger.debug(f"otel_bridge: {_LAST_STATUS_REASON}")
        return

    endpoint = config.get_endpoint()
    if not endpoint:
        _LAST_STATUS_REASON = (
            "otel_bridge_enabled=true but otel_bridge_endpoint is unset"
        )
        logger.warning(f"otel_bridge: {_LAST_STATUS_REASON}; skipping.")
        _emit_setup_banner(_LAST_STATUS_REASON)
        return

    installed_now = _auto_install_missing_deps()

    sdk = _try_import_otel_sdk()
    if sdk is None:
        _LAST_STATUS_REASON = (
            "otel_bridge_enabled=true but opentelemetry-sdk / "
            "opentelemetry-exporter-otlp-proto-http aren't installed"
        )
        logger.warning(
            f"otel_bridge: {_LAST_STATUS_REASON}. Run: "
            "pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http"
        )
        _emit_setup_banner(_LAST_STATUS_REASON)
        return
    (
        OTLPSpanExporter,
        Resource,
        TracerProvider,
        BatchSpanProcessor,
        set_tracer_provider,
    ) = sdk

    problem = _sdk_self_check(TracerProvider)
    if problem:
        if installed_now:
            # Observed in the wild (2026-07-04): packages installed
            # mid-process can leave Python's import state half-stale, so
            # the freshly-installed SDK flunks the self-check in THIS
            # process while being perfectly healthy on disk. Not a
            # version problem -- just needs a clean interpreter.
            _LAST_STATUS_REASON = (
                "tracing deps were just auto-installed; restart "
                "code-puppy to finish activating tracing"
            )
            logger.info(f"otel_bridge: {_LAST_STATUS_REASON}")
            _emit_notice(f"otel_bridge: {_LAST_STATUS_REASON}")
        else:
            _LAST_STATUS_REASON = problem
            logger.warning(f"otel_bridge: {_LAST_STATUS_REASON}")
            _emit_setup_banner(_LAST_STATUS_REASON)
        return

    try:
        from pydantic_ai import Agent, InstrumentationSettings
    except ImportError:
        # Shouldn't happen -- pydantic_ai is a code-puppy core dependency
        # -- but this plugin never assumes its host's internals can't
        # shift under it.
        _LAST_STATUS_REASON = (
            "pydantic_ai.Agent / InstrumentationSettings not importable"
        )
        logger.warning(f"otel_bridge: {_LAST_STATUS_REASON}; cannot instrument.")
        _emit_setup_banner(_LAST_STATUS_REASON)
        return

    resource = Resource.create({"service.name": config.get_service_name()})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint, headers=config.get_headers())
    provider.add_span_processor(BatchSpanProcessor(exporter))
    grouped = _try_add_baggage_processor(provider)
    set_tracer_provider(provider)

    Agent.instrument_all(InstrumentationSettings(tracer_provider=provider))

    _INSTRUMENTED = True
    grouping_note = f", session={SESSION_ID}" if grouped else " (no session grouping)"
    _LAST_STATUS_REASON = f"instrumented -> {endpoint}{grouping_note}"
    logger.info(f"otel_bridge: {_LAST_STATUS_REASON}")


async def _on_startup() -> None:
    """Host's ``startup`` callback -- config is guaranteed loaded by now.

    Runs in a worker thread: the dep auto-install inside _instrument()
    can block on a subprocess for seconds on first run, and the TUI's
    event loop shouldn't freeze for it.
    """
    await asyncio.to_thread(_instrument)


register_callback("startup", _on_startup)


# ---------------------------------------------------------------------------
# agent_run_context: tag every agent run with session/agent baggage
# ---------------------------------------------------------------------------


def _on_agent_run_context(agent: Any, pydantic_agent: Any, group_id: Any, mcp_servers: Any):
    """Return an async CM that scopes run-identifying baggage around run().

    The host composes the returned context manager (via AsyncExitStack)
    around ``pydantic_agent.run()`` -- see
    ``code_puppy.callbacks.on_agent_run_context``. Baggage attached here
    is copied onto every span the run creates by the
    BaggageSpanProcessor registered in :func:`_instrument`.

    Returns None (= host skips us) when instrumentation is off, so this
    hook is a true no-op for users who never enabled the bridge.
    """
    del pydantic_agent, mcp_servers
    if not _INSTRUMENTED:
        return None
    try:
        from opentelemetry import baggage
        from opentelemetry import context as otel_context
    except ImportError:  # pragma: no cover - api ships with pydantic-ai
        return None

    agent_name = str(getattr(agent, "name", "") or "")

    @contextlib.asynccontextmanager
    async def _tagged_run():
        ctx = baggage.set_baggage(_BAGGAGE_SESSION_KEY, SESSION_ID)
        if agent_name:
            ctx = baggage.set_baggage(
                f"{_BAGGAGE_PREFIX}agent", agent_name, context=ctx
            )
        if group_id:
            ctx = baggage.set_baggage(
                f"{_BAGGAGE_PREFIX}group_id", str(group_id), context=ctx
            )
        token = otel_context.attach(ctx)
        try:
            yield
        finally:
            otel_context.detach(token)

    return _tagged_run()


register_callback("agent_run_context", _on_agent_run_context)


# ---------------------------------------------------------------------------
# /otel-status: read-only diagnostic, no args, no side effects
# ---------------------------------------------------------------------------


@register_command(
    name="otel-status",
    description="Show whether otel_bridge has instrumented pydantic-ai and why/why not",
    usage="/otel-status",
    category="plugin",
)
def handle_otel_status_command(command: str) -> bool:
    del command
    from code_puppy.messaging import emit_info

    state = "ON" if _INSTRUMENTED else "off"
    emit_info(f"otel_bridge: {state} -- {_LAST_STATUS_REASON}")
    if not _INSTRUMENTED:
        emit_info(
            "Run /otel-setup for a guided fix (installs missing deps, walks "
            "through config, activates live). Keys are also settable "
            "directly via /set otel_bridge_<enabled|endpoint|headers|"
            "service_name> <value>."
        )
    return True
