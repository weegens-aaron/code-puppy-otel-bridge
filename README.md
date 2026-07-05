# otel_bridge

A [code-puppy](https://github.com/mpfaffenberger/code_puppy) plugin that
turns on pydantic-ai's native OpenTelemetry GenAI instrumentation and
ships the spans to any self-hosted, OTLP/HTTP-speaking observability
backend — Langfuse, Arize Phoenix, OpenLIT, SigNoz, an OTel Collector,
you name it. No code-puppy core changes, no vendor lock-in.

## What you get

- **Every agent run traced:** model calls, tool calls, token usage —
  pydantic-ai emits OTel GenAI semantic-convention spans natively; this
  plugin just flips the switch and routes the output.
- **Session grouping:** each code-puppy process gets a `session.id`
  (`code-puppy-<12 hex>`) attached to every span via OTel Baggage, so
  backends with a sessions feature (e.g. Langfuse) group the whole
  process — main agent, subagents, inspectors — into one browsable
  session.
- **Graceful degradation:** missing dependencies or config produce one
  log line and a no-op. The plugin never breaks agent work, plugin
  loading, or the interactive loop.

> **First time?** Follow the step-by-step
> [zero-to-traces setup guide](docs/SETUP.md) — it covers standing up a
> backend, every command to run, verification, and a troubleshooting
> table. The sections below are the condensed reference.

## Install

1. Clone into your code-puppy plugins directory (the `otel_bridge` name
   is required — the plugin loader keys off it):

   ```bash
   git clone https://github.com/weegens-aaron/code-puppy-otel-bridge.git ~/.code_puppy/plugins/otel_bridge
   ```

2. Start code-puppy and run **`/otel-setup`** — it walks you through
   the config keys and activates tracing live (no restart needed). If
   the plugin is enabled but can't trace, a startup banner points you
   here.

### Dependencies install themselves

Once `otel_bridge_enabled` is `true` and an endpoint is configured, the
plugin **auto-installs its three OTel packages at startup whenever
they're missing** — into whatever environment code-puppy is running
from (uvx cached env, a project venv, anything with `pip` or `uv`
reachable). Enabling tracing is the consent; disabled users get zero
network activity and zero environment changes.

That means uvx cache prunes, code-puppy version bumps, and fresh
per-project venvs all self-heal on the next launch (one "installing
missing tracing deps" line, a few seconds, done — occasionally followed
by a "restart code-puppy to finish activating tracing" notice when
Python can't hot-load the fresh packages into the running process). If you'd rather skip
even that occasional startup install, bake the deps into your launch
command:

```bash
uvx --with opentelemetry-sdk --with opentelemetry-exporter-otlp-proto-http --with opentelemetry-processor-baggage code-puppy
```

If the auto-install can't run (offline, no `pip`/`uv` reachable), the
plugin degrades gracefully as always — `/otel-status` reports what's
missing and `/otel-setup` retries on demand.

## Configure

Four config keys, all set via `/set <key> <value>` inside code-puppy
(they live in `puppy.cfg`):

| Key | Meaning | Default |
|---|---|---|
| `otel_bridge_enabled` | Master switch | `false` (off) |
| `otel_bridge_endpoint` | OTLP/HTTP traces endpoint URL | *(required)* |
| `otel_bridge_headers` | Comma-separated `Name=Value` headers (auth etc.) | *(none)* |
| `otel_bridge_service_name` | `service.name` resource attribute | `code-puppy` |

The guided way: run `/otel-setup` and follow the checklist — once every
key is green it activates instrumentation immediately, no restart. For
HTTP Basic auth backends, `/otel-setup auth <user> <secret>` computes
and stores the `Authorization` header for you.

Instrumentation is a one-way, process-lifetime switch: **changing config
after it's active requires a restart**. `/otel-status` shows the live
state at any time.

### Example: Langfuse (self-hosted)

With a Langfuse stack running (default UI/API on `http://localhost:3000`)
and a project's public/secret key pair:

```
/set otel_bridge_enabled true
/set otel_bridge_endpoint http://localhost:3000/api/public/otel/v1/traces
/set otel_bridge_headers Authorization=Basic <base64 of pk:sk>
```

Or run the helper script with the same Python environment as code-puppy:

```
python scripts/configure_puppy.py <base64-of-pk:sk> [otlp-endpoint]
```

Or, inside code-puppy (no base64 juggling, no restart):

```
/set otel_bridge_enabled true
/set otel_bridge_endpoint http://localhost:3000/api/public/otel/v1/traces
/otel-setup auth <public-key> <secret-key>
/otel-setup
```

Run any agent turn, and the trace appears in Langfuse — grouped under
the process's session.

Any other OTLP/HTTP backend works the same way: point
`otel_bridge_endpoint` at its traces endpoint and put whatever auth it
needs in `otel_bridge_headers`.

Something not working? See the
[troubleshooting table](docs/SETUP.md#troubleshooting).

## Behavior notes

- **Backend down?** Spans buffer briefly and the exporter logs export
  failures; agent work is unaffected (fire-and-forget batch export).
  Disable the plugin only if the log noise bothers you.
- **Privacy:** pydantic-ai's instrumentation includes prompt/completion
  content in spans by default. Only point this at backends you trust
  with that data.
- **Credentials:** the auth header is stored plaintext in `puppy.cfg`,
  and `/otel-setup auth` arguments persist in code-puppy's command
  history file like any typed command. Use least-privilege keys; see
  the [security notes](docs/SETUP.md#security--privacy-notes-read-before-pointing-at-a-shared-backend).
- **Enrichment from other plugins:** other plugins can add their own
  span attributes (custom session ids, run metadata) by attaching OTel
  Baggage in their own `agent_run_context` callback and registering
  their own narrowly-scoped `BaggageSpanProcessor`. This plugin's
  baggage allow-list stays deliberately narrow (`session.id` +
  `code_puppy.*`) — see `AGENTS.md` for the rationale.

## Development

- `pytest` from this directory runs the unit tests (no network, no
  backend needed).
- `python scripts/smoke_e2e.py <base64-of-pk:sk>` runs an offline agent
  through real instrumentation against a live Langfuse stack
  (`LANGFUSE_BASE_URL` env var overrides `http://localhost:3000`).
- Contributor docs, verified host contracts, and design rationale:
  `AGENTS.md` and `docs/research/`.

## License

[MIT](LICENSE)
