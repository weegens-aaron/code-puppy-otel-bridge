# Technical Research: Self-Hosted Agent Observability for code-puppy + bead_factory

- **Report date:** 2026-07-03 (system-derived)
- **Researcher:** Rowsdower (code-puppy-clone-1-a88e4e), via qa-kitten web retrieval (5 searches)
- **Constraint:** MUST NOT modify code-puppy core. Plugin-side and environment-side changes only.

## Executive Summary

- **Question:** What self-hosted agent observability can we implement for
  code-puppy and bead_factory without touching code-puppy proper?
- **Key Finding:** code-puppy core has **zero instrumentation code**, but its
  runtime -- **pydantic-ai 1.56.0** -- ships native OpenTelemetry GenAI
  instrumentation that a plugin can switch on globally with one call:
  `Agent.instrument_all()`. Every self-hosted backend evaluated (Langfuse,
  Arize Phoenix, OpenLIT, SigNoz) ingests standard OTLP, so the entire
  integration is: plugin registers an OTel `TracerProvider` + OTLP exporter ->
  calls `Agent.instrument_all()` -> points at the self-hosted backend's OTLP
  endpoint. No core surgery, no forking, no proxy.
- **Recommendation:** **Langfuse v3 self-hosted** (docker-compose) as primary
  choice -- practitioner-preferred agent tracing UX, an official pydantic-ai
  integration, a first-class OTLP endpoint, and MIT-licensed core. **Arize
  Phoenix** is the fallback if the Langfuse v3 four-datastore footprint
  (Postgres + ClickHouse + Redis + S3/MinIO) is too heavy for a dev box --
  Phoenix is a single container with SQLite, at the cost of an ELv2 license
  and a semantic-convention mismatch tax.

## Local Ground Truth (verified in this environment, 2026-07-03)

- `pydantic_ai` **1.56.0** installed; `logfire-api` and `opentelemetry-api`
  shims present; **no** `opentelemetry-sdk`, no OTLP exporter, no `langfuse`.
- `grep` of installed `code_puppy` package for
  `instrument|logfire|otel|opentelemetry`: **zero matches** -- core does not
  instrument and does not block instrumentation.
- The sibling `bead_factory` plugin demonstrates the plugin seam: it
  registers callbacks at plugin-load time (`register_callbacks.py`), so a
  module-scope (or `startup`-callback) `Agent.instrument_all(...)` call from
  plugin code runs before any agent turn.
- **This plugin's own verification, same session:** the host's `startup`
  callback phase and `agent_run_context` callback phase both exist in
  `code_puppy/callbacks.py` (grepped and read directly) -- `startup` fires
  once at app boot (config guaranteed loaded), `agent_run_context` wraps
  every `pydantic_agent.run()` call with plugin-supplied async context
  managers. See AGENTS.md "Remaining work" for how `agent_run_context` fits
  Phase 2.

## The Integration Seam (why "no core changes" is easy here)

Pydantic AI natively emits **OpenTelemetry GenAI semantic-convention spans**
(spec v1.37.0): `invoke_agent {gen_ai.agent.name}` spans, `execute_tool
{gen_ai.tool.name}` spans, `gen_ai.usage.input_tokens` /
`gen_ai.usage.output_tokens`, full message capture. Enablement is global and
retroactive -- from the API reference: *"instruments every Agent, existing
and future."*

```python
# From the official docs (#otel-without-logfire), verbatim pattern:
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import set_tracer_provider
from pydantic_ai import Agent

exporter = OTLPSpanExporter()  # honors OTEL_EXPORTER_OTLP_ENDPOINT / _HEADERS
tracer_provider = TracerProvider()
tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
set_tracer_provider(tracer_provider)
Agent.instrument_all()  # the only pydantic-ai line needed
```

Key `InstrumentationSettings` knobs (current signature, docs-verified):
`version` (default **5**; the old `event_mode` param is **gone** -- any doc
mentioning it is stale), `include_content=False` (strip prompts/completions
for privacy), `include_binary_content=False`,
`use_aggregated_usage_attribute_names` (pydantic-ai's custom
`gen_ai.aggregated_usage.*` namespace is **non-spec**; backends ignore it
unless told otherwise).

What changes and where:

| Change | Touches core? |
|---|---|
| `pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http` into code-puppy's env | No (environment) |
| This plugin's module that configures provider + `Agent.instrument_all()` | No (plugin) |
| `otel_bridge_endpoint` / `otel_bridge_headers` config keys | No (plugin config namespace) |
| bead_factory sets per-bead span attributes (Phase 2, separate repo) | No (bead_factory repo) |

## Options Found

### Option 1: Langfuse (self-hosted v3) -- RECOMMENDED

- **What it is:** Purpose-built LLM/agent observability: traces, sessions,
  scores, prompt management, evals.
- **GitHub:** https://github.com/langfuse/langfuse | ~30.4k stars | latest
  release `v3.205.0` published **2026-07-03** (day of this report) -- extremely
  active.
- **License:** MIT core; `ee/` directories under a separate enterprise
  license (open-core). GitHub API shows `NOASSERTION` -- the LICENSE file is
  authoritative.
- **Self-hosting:** `git clone && docker compose up` -> localhost:3000 in
  minutes (docs: langfuse.com/self-hosting/deployment/docker-compose, edited
  2026-03-20). **v3 footprint is real:** web + worker containers, Postgres,
  ClickHouse, Redis/Valkey, S3 (MinIO bundled). Postgres + ClickHouse must
  run UTC.
- **OTLP ingest:** Native endpoint `POST {host}/api/public/otel` (self-hosted
  requires >= v3.22.0). **HTTP only** (`http/protobuf` or `http/json`) --
  **no gRPC**. Auth: HTTP Basic from project keys, via standard env var:
  `OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic ${base64(pk:sk)}"`. No
  Langfuse SDK required.
- **Pydantic AI:** Official integration doc
  (langfuse.com/integrations/frameworks/pydantic-ai) -- listed as a
  first-class framework integration.
- **Trace grouping (the bead_factory win, Phase 2):** Langfuse maps plain OTel
  span attributes: `langfuse.session.id` (or `session.id`), `langfuse.user.id`,
  `langfuse.trace.tags`, and **filterable** `langfuse.trace.metadata.<key>`.
  Gotcha: attributes must be propagated to *every* span (docs recommend OTel
  Baggage + a `BaggageSpanProcessor`), and metadata NOT under the
  `langfuse.*.metadata.*` prefix lands in a non-filterable catch-all.
- **Tradeoffs:**
  - Practitioner-preferred agent-trace UX (JetBrains Koog team picked it over
    9 alternatives, 2025-12-12).
  - Zero-SDK OTLP path keeps this plugin's shim tiny.
  - Heaviest self-host stack of the purpose-built options (4 datastores).
  - Security history: authenticated prototype-pollution -> RCE disclosed
    2026-05-06 (AISafe Labs) against v3.167.4 via crafted OTel span attribute
    keys. For a single-user localhost deployment the exposure is low, but
    **pin a post-fix release** (>= current `v3.205.0`).
  - ClickHouse acquired Langfuse (2026-01-16). Self-hosting is unaffected;
    cloud/GDPR calculus changed. Watch stewardship.
- **Sources:** [2], [3], [4], [7], [8], [9]

### Option 2: Arize Phoenix -- lightest ops, license caveat

- **What it is:** LLM tracing + evals + datasets platform from Arize.
- **GitHub:** Arize-ai/phoenix | ~10.4k stars | `arize-phoenix v17.15.0`
  released 2026-07-01 -- very active.
- **License:** **Elastic License 2.0** (source-available, NOT OSI open
  source). Free and unrestricted for internal self-hosting; forbids offering
  it as a managed service. GitHub API shows `NOASSERTION` -- read the file.
- **Self-hosting:** Single Docker image (`arizephoenix/phoenix`), SQLite by
  default, Postgres for production. No Redis, no worker, no S3. Practitioner
  quote: *"A full observability stack is just a docker compose away."*
- **OTLP ingest:** `6006` HTTP (`/v1/traces`) + `4317` gRPC.
- **Pydantic AI:** Official doc, but via the
  `openinference-instrumentation-pydantic-ai` package -- Phoenix's UI expects
  **OpenInference** semantic conventions, not raw OTel GenAI semconv.
- **Tradeoffs:**
  - By far the lightest self-host; SQLite -> Postgres growth path.
  - Richest specialized span types (LLM/tool/chain/agent) *when fed
    OpenInference*.
  - Convention mismatch: pydantic-ai's native `Agent.instrument_all()` emits
    OTel GenAI semconv; Phoenix renders non-OpenInference spans as `unknown`
    (a real, complained-about UX bug -- HN, 2025-09). You'd add the
    OpenInference instrumentor instead of (or alongside) the native one.
  - ELv2 if an OSS-only policy ever applies.
- **Sources:** [5], [8]

### Option 3: OpenLIT -- cleanest license, quietest community

- **What it is:** OTel-native AI engineering platform (observability +
  guardrails, prompt hub, GPU monitoring).
- **GitHub:** openlit/openlit | ~2.6k stars | `openlit-1.22.0` released
  2026-06-10 -- active mono-repo.
- **License:** **Apache-2.0** -- cleanest of the field.
- **Self-hosting:** 3 containers (OpenLIT + ClickHouse + OTel Collector),
  docker-compose or Helm. Can reuse existing ClickHouse/Collector.
- **OTLP ingest:** Standard collector ports 4317/4318; renders traces from
  *any* OTel SDK -- pydantic-ai's native instrumentation works without
  OpenLIT's SDK.
- **Tradeoffs:** True OTel purity, no convention lock-in. Smallest community;
  barely surfaces in practitioner discussions; was evaluated and not picked
  in the JetBrains bake-off. Broader "platform" scope than needed here.
- **Sources:** [5], [8]

### Option 4: SigNoz -- one backend for everything, not agent-specialized

- **What it is:** General APM (traces/metrics/logs) on ClickHouse, with an
  LLM-observability docs section including a Pydantic AI page.
- **GitHub:** SigNoz/signoz | ~27.6k stars | `v0.131.1` released 2026-07-02 --
  very active. MIT core + `ee/` enterprise overlay.
- **Self-hosting:** Docker (>=4 GB RAM) via `foundryctl`; Windows host **not
  officially supported** (relevant if self-hosting on Windows -- WSL2/Docker
  Desktop required). Reference production deployment is famously heavy ("56
  CPU cores, 152 GiB RAM, 10 nodes" -- practitioner quote); Foundry exists
  precisely because of that criticism.
- **OTLP ingest:** Standard 4317/4318.
- **Tradeoffs:** Best if general infra observability is also wanted. Direct
  practitioner quote: *"A retrieval span, an LLM call, or a db transaction
  look all the same in Signoz. They don't render messages and tool calls any
  different."* For debugging /produce inspector loops, that's disqualifying
  as the primary UX.
- **Sources:** [5], [8]

### Non-options (checked, ruled out)

- **Pydantic Logfire self-hosted:** exists but **Enterprise-tier only**
  (Helm chart, Postgres + S3, contact-sales). No free self-host tier. [8]
- **Proxy-based tools (Helicone etc.):** would sit between code-puppy and
  model endpoints -- that's touching core config in spirit and adds a hop;
  OTel export is strictly cleaner given pydantic-ai's native support.

## Industry Perspective

No serious neutral bake-off exists in the tech press -- every "top 10 LLM
observability 2026" listicle is competitor content marketing. The credible
practitioner signal (JetBrains Koog engineering blog 2025-12-12; two large HN
threads 2025-09 and 2026-01) converges on:

1. **OTel is the consensus transport.** The debate ("Why OpenTelemetry Should
   Be the Standard", HN 2025-09) is about *semantic conventions*, not whether
   to use OTLP. Every option here ingests OTLP; instrumenting once via
   pydantic-ai keeps this plugin backend-portable.
2. **Langfuse wins developer UX for agent tracing** (JetBrains picked it over
   Phoenix, Opik, OpenLLMetry, Helicone, OpenLIT, Lunary, W&B Weave,
   LangSmith); **Phoenix wins span-type richness** *if* you adopt
   OpenInference; **SigNoz is a generalist** that renders LLM spans as raw
   JSON; **OpenLIT is legitimate but quiet**.
3. **Disagreement exists:** some practitioners call Phoenix "the best
   interface for doing this kind of work"; others "a clunky experience ...
   far happier with langfuse". Both quotes from the same HN thread [4] --
   treat UX preference as genuinely contested.

## Gaps and Caveats

- **Unverified locally:** that `Agent.instrument_all()` running from the
  `startup` callback precedes first agent construction in every code path
  (interactive REPL, headless/CLI invocation, subagents). `instrument_all`
  claims "existing and future" agents, which should make ordering moot -- but
  confirm with a live smoke test (see AGENTS.md "Remaining work").
- **Span volume/cost of `include_content=True`:** full prompt/completion
  capture on long-running agent sessions could be large; measure against
  your backend before enabling broadly. `include_content=False` is the
  escape hatch (loses the most useful debugging data, though).
- **Windows quirk:** Langfuse docker-compose was not smoke-tested on this
  machine; expected to work under Docker Desktop/WSL2 but unproven here.
- **Contested UX claims:** Langfuse-vs-Phoenix preference is practitioner
  opinion, not fact -- labeled as such above.
- **HN comments are Tier-4 sources:** used only for op-burden anecdotes and
  UX sentiment, each corroborated by Tier-1 docs where load-bearing.

## Sources

1. Pydantic -- "Debugging & Monitoring with Pydantic Logfire" and
   `InstrumentationSettings` API reference, accessed 2026-07-03.
   https://ai.pydantic.dev/logfire/ (-> pydantic.dev/docs/ai/integrations/logfire/),
   https://ai.pydantic.dev/api/models/instrumented/
2. Langfuse -- Self-hosting docs (v3) & Docker Compose guide (edited
   2026-03-20), accessed 2026-07-03. https://langfuse.com/self-hosting,
   https://langfuse.com/self-hosting/deployment/docker-compose
3. Langfuse -- "OpenTelemetry (native integration)" (edited 2026-05-18),
   accessed 2026-07-03. https://langfuse.com/integrations/native/opentelemetry
4. Langfuse -- "Pydantic AI integration", accessed 2026-07-03.
   https://langfuse.com/integrations/frameworks/pydantic-ai
5. Arize -- Phoenix self-hosting, configuration, and "Pydantic AI Tracing"
   docs, accessed 2026-07-03. https://arize.com/docs/phoenix/self-hosting,
   https://arize.com/docs/phoenix/integrations/python/pydantic/pydantic-tracing;
   OpenLIT docs https://docs.openlit.io/latest/openlit/installation;
   SigNoz docs https://signoz.io/docs/install/self-host/,
   https://signoz.io/docs/pydantic-ai-observability/
6. GitHub repos (stars/releases/licenses read 2026-07-03):
   langfuse/langfuse (30.4k stars, v3.205.0 2026-07-03, MIT core + ee/);
   Arize-ai/phoenix (10.4k stars, v17.15.0 2026-07-01, ELv2);
   openlit/openlit (2.6k stars, 1.22.0 2026-06-10, Apache-2.0);
   SigNoz/signoz (27.6k stars, v0.131.1 2026-07-02, MIT core + ee/)
7. Fineas Silaghi (AISafe Labs) -- "Traces of RCE: Exploiting Langfuse
   Prototype Pollution", 2026-05-06.
   https://aisafe.io/blog/traces-of-rce-exploiting-langfuse-prototype-pollution
8. Denis Domanskii (JetBrains AI Blog) -- "Building AI Agents in Kotlin --
   Part 3: Under Observation", 2025-12-12.
   https://blog.jetbrains.com/ai/2025/12/building-ai-agents-in-kotlin-part-3-under-observation/;
   HN threads: "ClickHouse acquires Langfuse" (2026-01-16,
   https://news.ycombinator.com/item?id=46656552), "LLM Observability in the
   Wild -- Why OpenTelemetry Should Be the Standard" (~2025-09,
   https://news.ycombinator.com/item?id=45398467);
   Pydantic pricing (Logfire Enterprise self-hosted), accessed 2026-07-03,
   https://pydantic.dev/pricing
9. Langfuse -- "Upgrade v2 -> v3" guide (v3 GA 2024-12-06), accessed
   2026-07-03. https://langfuse.com/self-hosting/upgrade/upgrade-guides/upgrade-v2-to-v3
