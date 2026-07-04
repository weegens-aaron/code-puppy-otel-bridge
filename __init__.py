"""otel_bridge: bridge pydantic-ai's native OpenTelemetry GenAI
instrumentation to a self-hosted OTLP-speaking observability backend
(Langfuse, Arize Phoenix, OpenLIT, SigNoz, an OTel Collector, ...).

This plugin does NOT touch code-puppy core. code-puppy's pydantic-ai
runtime already emits OTel GenAI semantic-convention spans natively
(``pydantic_ai.Agent.instrument_all()``); this plugin's entire job is to
flip that switch on at startup, config-gated, and point it at whatever
OTLP endpoint the user configured. See ``docs/research/`` for the
research report this design is built from, and ``AGENTS.md`` for scope,
the verified plugin-loader contract, and remaining/Phase 2 work.

Disabled by default (``otel_bridge_enabled`` config key). Degrades to a
single log line and a no-op if ``opentelemetry-sdk`` /
``opentelemetry-exporter-otlp-proto-http`` aren't installed, or if
pydantic_ai's instrumentation API isn't importable -- it never raises
into the host's plugin loader.
"""

# Single source of truth for the plugin version. This is the ONLY
# occurrence of the assignment in this file -- keep it that way so a
# non-Python build script (or test) can extract it with a plain grep.
__version__ = "0.2.0"
