# otel_bridge — zero-to-traces setup guide

Every step from "nothing installed" to "I can see my agent's trace in a
browser", plus a troubleshooting table for when reality disagrees with
the plan. The [README](../README.md) is the reference; this is the
walkthrough.

Langfuse is used as the worked example because it's the reference
backend this plugin was smoke-tested against — but any OTLP/HTTP
backend (Arize Phoenix, OpenLIT, SigNoz, an OTel Collector, ...) works
the same way: substitute its traces endpoint and auth in Steps 3–4.

## Prerequisites

- code-puppy installed and running (any install method: uv tool, uvx,
  pipx, plain venv).
- Docker (only for the local-Langfuse example backend in Step 1 — skip
  if you already have an OTLP backend).

## Step 1 — Stand up a backend (example: local Langfuse)

```bash
git clone https://github.com/langfuse/langfuse.git
cd langfuse
docker compose up -d
```

Wait for the containers to come up, then open <http://localhost:3000>:

1. Create an account (first user on a fresh stack).
2. Create an **organization** and a **project**.
3. In the project: **Settings → API Keys → Create new API keys**.
4. Copy the **public key** (`pk-lf-...`) and **secret key** (`sk-lf-...`).
   Keep them handy for Step 4.

Already running a different backend? All you need from it is:
- its **OTLP/HTTP traces endpoint URL** (usually ends in `/v1/traces`), and
- whatever **auth header** it wants (if any).

## Step 2 — Install the plugin

Clone it so it lives at exactly `~/.code_puppy/plugins/otel_bridge/`
(Windows: `%USERPROFILE%\.code_puppy\plugins\otel_bridge\`):

```bash
git clone https://github.com/weegens-aaron/code-puppy-otel-bridge.git ~/.code_puppy/plugins/otel_bridge
```

The loader keys off the directory name and `register_callbacks.py`
being present — don't rename either.

## Step 3 — Configure inside code-puppy

Start (or restart) code-puppy, then:

```
/set otel_bridge_enabled true
/set otel_bridge_endpoint http://localhost:3000/api/public/otel/v1/traces
```

> **Endpoint gotcha:** this must be the **full traces path**, not the
> bare host. For Langfuse that's
> `.../api/public/otel/v1/traces`; for most other backends it's
> `<base>/v1/traces`. A bare host URL will "work" silently and export
> nothing you can find.

## Step 4 — Auth + activate with `/otel-setup`

```
/otel-setup auth pk-lf-...your-public-key sk-lf-...your-secret-key
/otel-setup
```

- `auth` computes the HTTP Basic `Authorization` header and stores it —
  no manual base64. (Backend needs no auth? Skip this line.)
- The bare `/otel-setup` then runs the checklist: installs the three
  OTel packages into the running environment if they're missing, checks
  every config key, and — once all green — **activates tracing
  immediately**. No restart needed on this path.

If it prints a durability warning (uvx / uv tool / pipx environments),
your deps will survive *this* session but may vanish on the next tool
upgrade or cache prune. Do the durable install from the README's
[install table](../README.md#manual--durable-dependency-install) when
convenient — the warning message prints your exact command.

## Step 5 — Verify

1. Run any agent turn (ask code-puppy anything).
2. `/otel-status` should say `ON -- instrumented -> <your endpoint>,
   session=code-puppy-...`.
3. Open your backend's UI. In Langfuse: **Tracing → Traces** — your
   turn appears within a few seconds (spans batch-export on a ~5 s
   schedule). Under **Sessions**, the whole code-puppy process shows as
   one session named `code-puppy-<12 hex>`.

Seeing the trace? You're done. Not seeing it? Table below.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Startup banner: "tracing enabled but NOT active — endpoint is unset" | `otel_bridge_endpoint` never set | Step 3, then `/otel-setup` |
| Startup banner: "...opentelemetry-sdk ... aren't installed" | Deps missing from code-puppy's env (fresh install, tool upgrade, cache prune) | `/otel-setup` reinstalls them on the spot |
| Deps keep vanishing after every code-puppy upgrade | Non-durable env (uvx/uv tool/pipx) | Durable install: README [install table](../README.md#manual--durable-dependency-install) |
| `/otel-setup` says "no pip in this env and no `uv` on PATH" | Interpreter env has no installer available | Install `uv` (or run the `pip install` line it prints in whatever env code-puppy actually uses) |
| `/otel-status` says ON but nothing in the backend UI | Wrong endpoint path (bare host instead of full `/v1/traces` path) | Fix `otel_bridge_endpoint` per Step 3's gotcha, **restart code-puppy** |
| Same, plus export errors mentioning 401/403 in logs | Wrong or swapped keys | Re-run `/otel-setup auth <pk> <sk>` (public key first!), restart |
| Same, plus connection-refused export errors in logs | Backend not running / wrong port | `docker compose ps` in your Langfuse checkout; fix and traces resume on their own |
| Trace appears but only after a delay | Normal — batch exporter flushes on a ~5 s schedule | Patience, young pup |
| Traces arrive but aren't grouped into a session | `opentelemetry-processor-baggage` missing (partial install) | `/otel-setup` installs it; restart to re-instrument |
| Changed a config key, nothing happened | Instrumentation is a one-way, process-lifetime switch | Restart code-puppy after any config change made *after* activation |
| Startup banner annoys you and you don't want tracing | It only appears when `otel_bridge_enabled=true` | `/set otel_bridge_enabled false`, restart |

Still stuck? `/otel-status` always prints the exact reason
instrumentation is off — that string is the plugin's honest last word
on its own state.

## Security & privacy notes (read before pointing at a shared backend)

- **Span content:** pydantic-ai's instrumentation includes **prompt and
  completion content** (and tool call arguments — file paths, shell
  commands, etc.) in spans by default. Everything you and the model say
  ends up in the backend. Only point this plugin at backends you'd
  trust with the conversation itself.
- **Where credentials live:** the auth header is stored **in plaintext**
  in `~/.code_puppy/puppy.cfg` — same trust level as the rest of your
  code-puppy config. Use project-scoped, least-privilege keys (a
  Langfuse project key can only write/read that project's traces), and
  don't reuse credentials that guard anything else.
- **Command history:** anything you type — including
  `/otel-setup auth <pk> <sk>` — is saved to
  `~/.code_puppy/command_history.txt`. The command warns you about this
  when you use it. On shared machines, scrub that line afterwards, or
  set the header without typing secrets into the REPL:
  `python scripts/configure_puppy.py <b64>`. Note your *shell's* history
  then holds the secret instead — a leading space omits the command in
  bash (`HISTCONTROL=ignorespace`, default on many distros) and zsh
  (`setopt HIST_IGNORE_SPACE`), but **not** in PowerShell or cmd; there,
  clear it with `Clear-History` plus deleting the PSReadLine file
  (`(Get-PSReadLineOption).HistorySavePath`).
- **Baggage stays narrow:** the plugin only attaches non-sensitive
  identifiers (random session id, agent name, group id) as OTel Baggage,
  and only copies its own allow-listed keys onto spans — arbitrary
  baggage from other libraries is never forwarded to the backend.
