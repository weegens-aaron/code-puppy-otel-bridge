"""Dev tool: empirically discover which span attributes THIS Langfuse
instance maps to trace Input/Output.

Emits one root span per candidate attribute set to the configured OTLP
endpoint (read from puppy.cfg via code_puppy.config, same as the plugin),
then queries the Langfuse public API for each trace and reports whether
`input` / `output` populated.

Usage: python scripts/debug_langfuse_mapping.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from code_puppy.config import get_value

ENDPOINT = get_value("otel_bridge_endpoint")
HEADERS_RAW = get_value("otel_bridge_headers") or ""
HEADERS = dict(
    pair.strip().partition("=")[::2]
    for pair in HEADERS_RAW.split(",")
    if "=" in pair
)
LANGFUSE_BASE = "http://localhost:3000"

USER_MSG = [{"role": "user", "parts": [{"type": "text", "content": "test input"}]}]
ASSISTANT_MSG = [
    {"role": "assistant", "parts": [{"type": "text", "content": "test output"}]}
]
V1_EVENTS = [
    {"content": "test input", "role": "user"},
    {"content": "test output", "role": "assistant"},
]

CASES = {
    "expA-v2-root-shape": {  # what pydantic-ai v2 puts on the agent-run root span
        "pydantic_ai.all_messages": json.dumps(USER_MSG + ASSISTANT_MSG),
        "final_result": "test output",
    },
    "expB-v1-root-shape": {  # pydantic-ai v1 root span shape
        "all_messages_events": json.dumps(V1_EVENTS),
        "final_result": "test output",
    },
    "expC-genai-messages": {  # OTel GenAI semconv input/output messages
        "gen_ai.input.messages": json.dumps(USER_MSG),
        "gen_ai.output.messages": json.dumps(ASSISTANT_MSG),
    },
    "expD-plain-input": {
        "input": json.dumps(USER_MSG),
        "output": "test output",
    },
    "expE-langfuse-native": {
        "langfuse.observation.input": json.dumps(USER_MSG),
        "langfuse.observation.output": "test output",
    },
}


def main() -> int:
    provider = TracerProvider(
        resource=Resource.create({"service.name": "otel-bridge-mapping-probe"})
    )
    exporter = OTLPSpanExporter(endpoint=ENDPOINT, headers=HEADERS)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    tracer = provider.get_tracer("mapping-probe")

    trace_ids: dict[str, str] = {}
    for name, attrs in CASES.items():
        with tracer.start_as_current_span(name, attributes=attrs) as span:
            trace_ids[name] = format(span.get_span_context().trace_id, "032x")
    provider.force_flush(timeout_millis=15000)

    print(f"{'case':<24} {'input?':<26} output?")
    print("-" * 72)
    deadline = time.time() + 60
    pending = dict(trace_ids)
    while pending and time.time() < deadline:
        for name, tid in list(pending.items()):
            req = urllib.request.Request(
                f"{LANGFUSE_BASE}/api/public/traces/{tid}",
                headers={"Authorization": HEADERS.get("Authorization", "")},
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    t = json.loads(resp.read().decode())
            except Exception:
                continue  # not ingested yet; retry
            def _show(v):
                if v is None:
                    return "EMPTY"
                s = json.dumps(v) if not isinstance(v, str) else v
                return s[:24]
            print(f"{name:<24} {_show(t.get('input')):<26} {_show(t.get('output'))}")
            del pending[name]
        if pending:
            time.sleep(3)

    for name in pending:
        print(f"{name:<24} <never ingested within 60s>")
    return 1 if pending else 0


if __name__ == "__main__":
    sys.exit(main())
