"""Dev tool: dump the span attributes pydantic-ai emits per instrumentation version.

Runs a TestModel agent (offline) under InstrumentationSettings(version=N)
for each supported version and prints every span's attribute keys (values
truncated), so we can see exactly which attribute carries the user input
-- and therefore what a trace backend can or cannot map.

Usage: python scripts/debug_span_attrs.py
"""

from __future__ import annotations

import asyncio

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


def dump_for_version(version: int) -> None:
    from pydantic_ai import Agent
    from pydantic_ai.models.instrumented import InstrumentationSettings
    from pydantic_ai.models.test import TestModel

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    settings = InstrumentationSettings(tracer_provider=provider, version=version)
    agent = Agent(TestModel(), name="debug-agent", instrument=settings)

    asyncio.run(agent.run("What is the airspeed velocity of an unladen swallow?"))

    print(f"\n{'=' * 20} version={version} {'=' * 20}")
    for span in exporter.get_finished_spans():
        parent = span.parent.span_id if span.parent else None
        print(f"\n  span: {span.name!r} (parent={'root' if parent is None else 'child'})")
        for key, value in sorted((span.attributes or {}).items()):
            text = str(value)
            if len(text) > 120:
                text = text[:120] + f"... ({len(text)} chars)"
            print(f"    {key} = {text}")


if __name__ == "__main__":
    for v in (1, 2, 3):
        dump_for_version(v)
