"""End-to-end smoke test: otel_bridge -> real OTel SDK -> real Langfuse.

Run manually (not part of pytest -- needs a Langfuse stack reachable at
``LANGFUSE_BASE_URL``, default http://localhost:3000):

    python scripts/smoke_e2e.py <BASIC_AUTH_B64>

``BASIC_AUTH_B64`` is ``base64(<public-key>:<secret-key>)`` for your
Langfuse project. Override the stack location with the
``LANGFUSE_BASE_URL`` environment variable if it isn't on localhost.

What it proves, in order:
  1. ``_instrument()`` succeeds against the real opentelemetry-sdk with
     config pointing at the local Langfuse OTLP endpoint.
  2. A real ``pydantic_ai.Agent`` run (TestModel -- no LLM network) emits
     spans through the instrumented provider.
  3. The ``agent_run_context`` CM attaches session baggage and the
     BaggageSpanProcessor copies it onto the spans.
  4. Langfuse ingested the trace: the public REST API returns it, tagged
     with this process's SESSION_ID.

Exit code 0 + "SMOKE PASS" on success; nonzero with a reason otherwise.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import urllib.request

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS_DIR = os.path.dirname(PLUGIN_DIR)
for p in (PLUGINS_DIR, PLUGIN_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

LANGFUSE_BASE = os.environ.get("LANGFUSE_BASE_URL", "http://localhost:3000").rstrip("/")
OTLP_ENDPOINT = f"{LANGFUSE_BASE}/api/public/otel/v1/traces"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python scripts/smoke_e2e.py <BASIC_AUTH_B64>")
        return 2
    auth_b64 = sys.argv[1].strip()

    from otel_bridge import config as ob_config
    from otel_bridge import register_callbacks as rc

    # Point the plugin's config reads at the local stack (in-memory only;
    # puppy.cfg is not touched by this script).
    values = {
        "otel_bridge_enabled": "true",
        "otel_bridge_endpoint": OTLP_ENDPOINT,
        "otel_bridge_headers": f"Authorization=Basic {auth_b64}",
        "otel_bridge_service_name": "otel-bridge-smoke",
    }
    ob_config.get_value = lambda key: values.get(key)  # type: ignore[assignment]

    # 1. Real instrumentation.
    rc._instrument()
    if not rc._INSTRUMENTED:
        print(f"SMOKE FAIL: _instrument() did not engage: {rc._LAST_STATUS_REASON}")
        return 1
    print(f"instrumented: {rc._LAST_STATUS_REASON}")

    # 2 + 3. Real agent run (TestModel = deterministic, offline) inside
    # the plugin's own agent_run_context CM, exactly as the host composes
    # it around pydantic_agent.run().
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    agent = Agent(TestModel(), name="smoke-agent")

    class _HostAgentShim:
        name = "smoke-agent"

    async def _run():
        cm = rc._on_agent_run_context(_HostAgentShim(), agent, "smoke-group-1", [])
        assert cm is not None, "agent_run_context returned None while instrumented"
        async with cm:
            result = await agent.run("Say hello for the smoke test.")
        return result

    result = asyncio.run(_run())
    print(f"agent ran, output: {str(result.output)[:60]!r}")

    # Flush: BatchSpanProcessor is async; force it before querying.
    from opentelemetry import trace as otel_trace

    provider = otel_trace.get_tracer_provider()
    flushed = provider.force_flush(timeout_millis=15000)
    print(f"force_flush -> {flushed}")

    # 4. Verify ingestion via Langfuse public API (Basic auth, pk:sk).
    deadline = time.time() + 60
    session_traces = []
    while time.time() < deadline:
        req = urllib.request.Request(
            f"{LANGFUSE_BASE}/api/public/traces?sessionId={rc.SESSION_ID}",
            headers={"Authorization": f"Basic {auth_b64}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode())
            session_traces = payload.get("data", [])
            if session_traces:
                break
        except Exception as exc:  # noqa: BLE001 - report and retry
            print(f"  (query retry: {exc})")
        time.sleep(3)

    if not session_traces:
        print(
            "SMOKE FAIL: no trace with sessionId="
            f"{rc.SESSION_ID} appeared in Langfuse within 60s"
        )
        return 1

    t = session_traces[0]
    print(
        "SMOKE PASS: trace ingested -- "
        f"id={t.get('id')} name={t.get('name')!r} sessionId={t.get('sessionId')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
