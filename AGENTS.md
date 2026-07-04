# AGENTS.md -- otel_bridge

## Why this exists

Goal: self-hosted agent observability for code-puppy (and any plugin
running on it) without touching code-puppy core. pydantic-ai (code-puppy's
agent runtime) natively emits OpenTelemetry GenAI spans, switchable
globally with `pydantic_ai.Agent.instrument_all()`. This plugin flips
that switch on, config-gated, and ships spans to whatever self-hosted
OTLP backend you point it at. It is a plugin, not a core patch, and it
stays that way. (Research:
`docs/research/self-hosted-agent-observability-code-puppy.md`.)

## Scope (SRP -- read this before adding features)

This plugin's ONE job: turn on pydantic-ai's native OTel instrumentation
and route it to a configured OTLP endpoint. It knows nothing about beads,
`/factory`, or other plugins. Richer per-run tagging (session grouping,
custom tags) belongs in the plugin that wants it, via the
`agent_run_context` hook described below -- not bolted onto otel_bridge.

## Status: COMPLETE and smoke-tested E2E (v0.3.0)

**Phase 3 (done):** guided onboarding. When the bridge is enabled but
can't trace, `_emit_setup_banner` (in `register_callbacks.py`) shows one
visible `emit_warning` line at startup pointing at **`/otel-setup`**
(`setup_command.py`). That command: (a) detects missing runtime deps via
`find_spec` probes matching the exact imports `_instrument()` performs,
(b) installs them into the running env (`pip` if the env has it, else
`uv pip install --python <sys.executable>`), warning when the env is
uv-managed and therefore non-durable (the supported launch scenario is
`uvx code-puppy`),
(c) reports each config key's state with the exact `/set` command to run
next, and (d) once everything is green, calls `_instrument()` live --
idempotent + one-way, so the happy path needs **no restart**.
`/otel-setup auth <user> <secret>` writes an HTTP Basic `Authorization`
header via `config.set_headers` (the plugin's ONLY config write, always
user-initiated; `otel_bridge_enabled` is still never flipped from code).
Header VALUES are never echoed to output -- only key names. Command
handlers receive the full command string (verified:
`command_handler.py` dispatches `cmd_info.handler(command)`).

**Phase 1 (done):** global instrumentation. `register_callbacks.py`
registers on the host's `startup` callback phase, reads config, builds a
`TracerProvider` + `OTLPSpanExporter`, calls `Agent.instrument_all()`.
Fully defensive -- missing deps or config log one line and skip; never
raises.

**Phase 2 (done):** per-run session/trace grouping. A process-wide
`SESSION_ID` (`code-puppy-<12 hex>`) is attached as OTel Baggage --
along with `code_puppy.agent` and `code_puppy.group_id` -- by an
`agent_run_context` callback (async CM composed by the host around every
`pydantic_agent.run()`; signature `(agent, pydantic_agent, group_id,
mcp_servers) -> async CM | None`, verified in `code_puppy/callbacks.py`).
A `BaggageSpanProcessor` (package `opentelemetry-processor-baggage`,
verified on PyPI at 0.64b0, import
`from opentelemetry.processor.baggage import BaggageSpanProcessor`) copies
those baggage entries onto every span at creation. The predicate is a
**narrow allow-list** (`_baggage_key_allowed`): only `session.id` and
`code_puppy.*`. Never widen it to `ALLOW_ALL_BAGGAGE_KEYS` -- W3C Baggage
propagates into outbound HTTP headers of instrumented clients and can
arrive from anywhere; copying arbitrary baggage onto spans leaks junk
(or worse) into the backend. Langfuse maps the generic `session.id` span
attribute to its Sessions feature: one code-puppy process = one
browsable Langfuse session containing all agent/subagent traces.

**E2E smoke (passed 2026-07-04):** `scripts/smoke_e2e.py` runs
`_instrument()` against the real SDK, executes a real `pydantic_ai.Agent`
(TestModel, offline) inside the plugin's `agent_run_context` CM,
force-flushes, and confirms via Langfuse's REST API that the trace
arrived with the expected `sessionId`.

**60 unit tests** (`pytest` from this directory; note: code-puppy's env
may lack pytest -- `uv pip install --python <code-puppy-python> pytest`).
Degrade paths use stubs; baggage tests use the real OTel API
deliberately; messaging emitters are stubbed via
`monkeypatch.setattr(code_puppy.messaging, "emit_*", ...)` (the lazy
in-function imports pick patches up at call time).

## Ops notes (example backend: local Langfuse stack)

User-facing setup lives in `README.md` (condensed reference) and
`docs/SETUP.md` (zero-to-traces walkthrough + troubleshooting table);
keep all three in sync. Dev notes:

- **Reference stack:** Langfuse v3 via its official docker-compose
  (`docker compose up -d` in a Langfuse checkout; UI on
  http://localhost:3000 by default). Any OTLP/HTTP backend works -- this
  plugin is backend-agnostic.
- **Plugin config:** the four `otel_bridge_*` keys live in `puppy.cfg`
  (`scripts/configure_puppy.py <basic-auth-b64> [endpoint]` writes them;
  for Langfuse the b64 string is `base64(pk:sk)`). `/otel-status` inside
  code-puppy shows live state. Instrumentation happens once at startup --
  restart code-puppy after config changes.
- **If the backend is down** while code-puppy runs: spans buffer briefly
  and the OTLP exporter logs export failures; agent work is unaffected
  (BatchSpanProcessor is fire-and-forget). `/set otel_bridge_enabled
  false` + restart silences the log noise if it bothers you.

## The verified plugin-loader contract (why this repo is shaped this way)

Read straight from `code_puppy/plugins/__init__.py` this session:

- User plugins live at `~/.code_puppy/plugins/<name>/` (this directory).
  The loader (`_load_user_plugins`) looks for `register_callbacks.py` in
  each subdirectory and loads it via `importlib.util.spec_from_file_location`
  -- NOT a normal package import. This means:
  - **Relative imports work** (`from . import config`) because the
    loader sets `module_name = f"{plugin_name}.register_callbacks"`
    before executing it, so Python resolves `.` against the synthetic
    `otel_bridge` module registered in `sys.modules`.
  - Under bare `pytest` (not through the host loader) you need
    `~/.code_puppy/plugins` on `sys.path` so `import otel_bridge` resolves
    as a plain top-level package instead. `tests/conftest.py` does this
    (the standard conftest pattern for code-puppy user plugins).
- A directory is only picked up if it contains `register_callbacks.py`
  (or `__init__.py` alone as a fallback, with no registrations). This
  plugin has both; `register_callbacks.py` is the real entry point.
- Import failures are caught and logged by the loader, not fatal to the
  rest of code-puppy -- but don't rely on that; this plugin's own code
  never raises out of `register_callbacks.py` at import time regardless.
- Plugin names collide by directory name across three tiers (builtin /
  user / project; project wins, then user). `otel_bridge` was verified
  collision-free against the builtin plugins list.

## Verified config API (`code_puppy.config`)

- `get_value(key: str) -> str | None` -- reads `puppy.cfg`, `None` if
  unset. No type coercion; plugins do their own parsing (see `config.py`).
- `set_config_value(key, value)` / alias `set_value(key, value)` -- both
  write `puppy.cfg`.
- Users set config via the host's `/set <key> <value>` command.
- Config keys owned by this plugin (all namespaced `otel_bridge_*`, see
  `config.py` docstring for full semantics): `otel_bridge_enabled`,
  `otel_bridge_endpoint`, `otel_bridge_headers`, `otel_bridge_service_name`.

## Verified pydantic-ai OTel API (do not re-derive from memory -- verified against the INSTALLED lib)

Verified against pydantic-ai **1.56.0 as installed in code-puppy's env**
(2026-07-04) -- which differs from the 2026-07-03 docs snapshot this
section previously trusted (docs described a newer release):

- `pydantic_ai.Agent.instrument_all(settings=None)` -- classmethod,
  instruments every `Agent` instance, existing and future, in the
  process. No need to touch code that constructs agents.
- `InstrumentationSettings(*, tracer_provider=None, meter_provider=None,
  include_binary_content=True, include_content=True,
  version: Literal[1, 2, 3] = 2, event_mode='attributes', ...)` --
  `version` default is **2** here (not 5), and `event_mode` still
  exists in this release. Trust `inspect.signature` over docs snapshots.
- Span shape at version 2 (dump with `scripts/debug_span_attrs.py`):
  root `agent run` span carries `pydantic_ai.all_messages` (single JSON
  string) + `final_result`; child `chat <model>` spans carry semconv
  `gen_ai.input.messages` / `gen_ai.output.messages`.
- Full detail and the "OTel without Logfire" pattern:
  `docs/research/self-hosted-agent-observability-code-puppy.md`.

## Langfuse mapping facts (empirical, 2026-07-04 -- read before "fixing" blank UI columns)

Established with `scripts/debug_langfuse_mapping.py` (synthetic attribute
matrix) and API autopsies of real traces against the local Langfuse v3:

- **Trace input was never missing.** Real agent-run traces carry full
  input (13 KB - 300 KB message arrays) retrievable via
  `GET /api/public/traces/{id}`. The blank "Input" column in the
  Tracing TABLE is a UI preview quirk for large structured inputs; the
  trace detail view has the data.
- This Langfuse maps pydantic-ai's native root-span attrs directly:
  `pydantic_ai.all_messages` -> trace input, `final_result` -> trace
  output -- but (from probe spans without pydantic-ai's instrumentation
  scope) generic `gen_ai.input.messages`/`gen_ai.output.messages`,
  `input`/`output`, and `langfuse.observation.*` all map too, while a
  hand-set `pydantic_ai.all_messages` did NOT (their pydantic-ai mapping
  appears scope/shape-gated). When both native and semconv attrs are
  present, Langfuse prefers the native pydantic-ai ones.
- **A `gen_ai.input/output.messages` promotion processor was built,
  E2E-verified, and then REVERTED** (2026-07-04): redundant for Langfuse
  (native mapping already covers it) and it duplicates up to ~300 KB per
  root span. Don't re-add it without a concrete backend that needs it
  AND a size guard.

## Remaining work (in priority order)

1. **Dependency durability (mitigated, not solved).** Supported launch
   scenario: **`uvx code-puppy`, always** (owner decision 2026-07-04;
   other install methods are explicitly the user's own problem -- don't
   re-add per-method tables/heuristics). The three runtime deps
   (`opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`,
   `opentelemetry-processor-baggage`) must live in code-puppy's env;
   uvx rebuilds its cached env WITHOUT them on cache prune or version
   change. The plugin then degrades gracefully AND (when enabled)
   banners at startup pointing at `/otel-setup`, which reinstalls into
   the running cached env on the spot and prints the durable
   incantation: `uvx --with <each dep> code-puppy`. No durable fix
   possible from inside the plugin; keep the README's install section
   accurate.
2. **Sibling-plugin enrichment pattern: PROVEN (2026-07-04).** Another
   plugin added its own narrowly-scoped BaggageSpanProcessor to the
   global provider to emit backend-specific session/metadata baggage
   (e.g. `langfuse.session.id` per task-run) from its own
   `agent_run_context` callback. otel_bridge's allow-list stayed narrow
   and no otel_bridge code changed -- exactly the division of labor the
   Scope section prescribes. Note: Langfuse gives `langfuse.session.id`
   precedence over generic `session.id`, so enriched runs group as their
   own sessions while ordinary turns keep the process session.
3. **Optional polish, only if wanted:** config knobs for
   `include_content=False` (privacy: strip prompts/completions from
   spans) and a `/set`-able instrumentation `version`. YAGNI until
   someone asks. (Guided onboarding -- banner + `/otel-setup` -- shipped
   in v0.3.0, see Status.)

## Security posture (audited pre-release; keep these invariants)

Audit findings + invariants any future change must preserve:

- **Never echo credential values.** Header VALUES don't appear in any
  emit/log/print -- only key names (`/otel-setup` walkthrough) or
  `<set, value hidden>` (`configure_puppy.py`). Pinned by test
  (`test_walkthrough_activates_live_when_fully_configured`).
- **`/otel-setup auth` secrets persist in the host's
  `~/.code_puppy/command_history.txt`** (verified: file exists; typed
  commands are saved verbatim). Can't be prevented from a plugin; the
  command emits a warning about it and SETUP.md documents it. Don't
  remove that warning.
- **Plaintext storage in `puppy.cfg` is by design** (same trust level as
  the rest of code-puppy's config); docs steer users to least-privilege
  project-scoped keys.
- **Local tooling state never ships:** `.beads/`, `.pytest_cache/`,
  `__pycache__/` are gitignored. Repo had no commits at audit time, so
  no history scrubbing was needed. Working tree verified free of real
  keys, personal paths, emails, and non-localhost private URLs.
- **Subprocess use is injection-safe:** installer invocations are argv
  lists (`shell=False`), with timeouts; never interpolate user input
  into a shell string.
- **Baggage allow-list stays narrow** (see Scope) -- that's a security
  property, not just tidiness: baggage values become span attributes in
  the backend.

## Cross-platform invariants (Windows / Linux / macOS)

- All path handling is `os.path`-based; the ONLY platform-heuristic code
  is `setup_command.durability_note()`, which normalizes separators and
  detects uv-managed envs (`/uv/` in the interpreter path) across the
  uvx cache locations of all three OSes -- pinned by a 6-case
  parametrized test. It's advisory-only (a warning, never a gate), so
  exotic `UV_CACHE_DIR` values defeating it is acceptable degradation.
- Installer subprocess uses `encoding="utf-8", errors="replace"` --
  Windows' locale codec (cp1252) would otherwise raise mid-install on
  pip/uv's UTF-8 output. Don't remove.
- User-facing emit/log strings stay ASCII (console codepages vary).
- `.gitattributes` normalizes line endings (`* text=auto`, `*.py eol=lf`).
- Shell-history advice in SETUP.md is per-shell (bash/zsh vs PowerShell)
  -- keep it accurate per platform, not "most shells" hand-waving.

## Ground rules

- Stay backend-agnostic. No Langfuse-specific config keys beyond generic
  `otel_bridge_headers` -- the whole point of building on OTLP is not
  getting locked into one vendor. If you add backend-specific
  conveniences, gate them behind their own clearly-named keys and keep
  the generic path working without them.
- Every optional dependency (OTel SDK, exporter, anything Phase 2 needs)
  must degrade gracefully: log one clear line, never raise, never break
  plugin loading or the interactive loop. Match the `_try_import_otel_sdk`
  pattern already in `register_callbacks.py`.
- Disabled by default. Never flip `otel_bridge_enabled` on programmatically.
- Keep files under 600 lines; this plugin is small on purpose (YAGNI --
  don't add a config UI, a dashboard, or multi-backend routing until
  something real needs it).
- If you change the config key surface or the callback hooks used, update
  this file in the same change -- a stale AGENTS.md is a bug, not history.
