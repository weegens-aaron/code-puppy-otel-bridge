"""``/otel-setup``: install missing OTel deps and walk through config.

The companion to the startup banner in :mod:`register_callbacks`: when
otel_bridge is enabled but can't instrument (missing deps, missing
config), the banner points users here, and this command fixes what it
can and prints the exact next step for what it can't.

Two modes:

``/otel-setup``
    Checklist walkthrough. Installs any missing OTel packages into the
    running interpreter's environment (via ``pip`` if the env has it,
    else ``uv pip install --python <this-python>``), reports each config
    key's state, and -- once everything is green -- activates
    instrumentation live via ``register_callbacks._instrument()``
    (idempotent, one-way), so no restart is needed on the happy path.

``/otel-setup auth <user> <secret>``
    Convenience for HTTP Basic auth backends (e.g. Langfuse public/secret
    keys): computes ``base64(user:secret)`` and writes
    ``otel_bridge_headers=Authorization=Basic <b64>``. Generic Basic
    auth, not vendor-specific, so it stays inside the backend-agnostic
    ground rule.

Durability caveat (see AGENTS.md "Remaining work" #1): the supported
launch scenario is ``uvx code-puppy``, whose cached env is rebuilt
without these deps on cache prune / version change -- installing into
it is NOT durable. :func:`durability_note` detects uv-managed envs and
prints the durable ``uvx --with`` incantation so users aren't surprised
later. Other install methods work but are the user's own concern.
"""

from __future__ import annotations

import base64
import importlib
import importlib.util
import logging
import shutil
import subprocess
import sys

from code_puppy.command_line.command_registry import register_command

from . import config

logger = logging.getLogger(__name__)

__all__ = ["handle_otel_setup_command", "missing_deps"]

# (probe import name, pip distribution name). The probe modules match
# the exact imports register_callbacks performs, so "not missing" here
# really means "_instrument() will find them".
DEPS = (
    ("opentelemetry.sdk.trace", "opentelemetry-sdk"),
    (
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "opentelemetry-exporter-otlp-proto-http",
    ),
    ("opentelemetry.processor.baggage", "opentelemetry-processor-baggage"),
)

_DEP_NAMES = tuple(dist for _probe, dist in DEPS)
_INSTALL_TIMEOUT_S = 300


def missing_deps() -> list[str]:
    """Pip distribution names whose probe module can't be found."""
    missing: list[str] = []
    for probe, dist in DEPS:
        try:
            found = importlib.util.find_spec(probe) is not None
        except (ImportError, ValueError):
            # find_spec raises ModuleNotFoundError when a PARENT package
            # is absent, and ValueError on weird __spec__ states.
            found = False
        if not found:
            missing.append(dist)
    return missing


def _install_argv(packages: list[str]) -> list[str] | None:
    """Pick an installer for THIS interpreter's env, or None if stuck.

    uv-built envs usually ship without pip, so ``uv pip install
    --python <sys.executable>`` is the fallback -- it targets the exact
    env this process (and therefore this plugin) imports from.
    """
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install", *packages]
    if shutil.which("uv"):
        return ["uv", "pip", "install", "--python", sys.executable, *packages]
    return None


def install_deps(packages: list[str]) -> tuple[bool, str]:
    """Install ``packages`` into the running env. Returns (ok, detail)."""
    argv = _install_argv(packages)
    if argv is None:
        return False, (
            "no pip in this env and no `uv` on PATH -- install manually: "
            f"pip install {' '.join(packages)}"
        )
    try:
        # Explicit utf-8 + replace: pip/uv emit UTF-8 regardless of the
        # console codepage, and Windows' default locale codec (cp1252)
        # would raise UnicodeDecodeError mid-install otherwise.
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_INSTALL_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"installer failed to run: {exc}"
    importlib.invalidate_caches()
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-5:]
        return False, (
            f"installer exited {result.returncode}:\n" + "\n".join(tail)
        )
    return True, " ".join(argv)


def durability_note() -> str | None:
    """Warn that a uv-managed env won't survive tool maintenance.

    The supported way to run code-puppy is ``uvx code-puppy``, whose
    cached env is rebuilt WITHOUT these deps on cache prune or version
    change. Any uv-managed interpreter gets the durable ``uvx --with``
    incantation; other setups are off the paved path and get no note
    (the install still works -- durability is their own concern).
    """
    exe = sys.executable.replace("\\", "/").lower()
    if "/uv/" not in exe:
        return None
    with_flags = " ".join(f"--with {d}" for d in _DEP_NAMES)
    return (
        "uv-managed env detected -- this install vanishes on cache prune "
        "or code-puppy version change. Durable form (alias it!): "
        f"uvx {with_flags} code-puppy"
    )


def _handle_auth(args: list[str]) -> bool:
    from code_puppy.messaging import (
        emit_error,
        emit_info,
        emit_success,
        emit_warning,
    )

    if len(args) != 2:
        emit_error(
            "usage: /otel-setup auth <user-or-public-key> <password-or-secret-key>"
        )
        return True
    user, secret = args
    token = base64.b64encode(f"{user}:{secret}".encode()).decode()
    config.set_headers(f"Authorization=Basic {token}")
    emit_success("otel_bridge_headers set (HTTP Basic auth).")
    emit_warning(
        "Heads up: like any typed command, the keys you just entered are "
        "saved to ~/.code_puppy/command_history.txt (and the header lives "
        "in puppy.cfg). On a shared machine, scrub the history line and "
        "prefer project-scoped, least-privilege keys."
    )
    emit_info("Run /otel-setup to continue the walkthrough.")
    return True


def _check_deps(emitters) -> bool:
    """Step 1 of the walkthrough. Returns False if setup can't continue."""
    emit_error, emit_info, emit_success, emit_warning = emitters
    missing = missing_deps()
    if not missing:
        emit_success("[1/4] deps: all installed")
        return True
    emit_warning(f"[1/4] deps: missing {', '.join(missing)} -- installing...")
    ok, detail = install_deps(missing)
    if not ok:
        emit_error(f"[1/4] deps: install failed -- {detail}")
        return False
    still = missing_deps()
    if still:
        emit_error(
            f"[1/4] deps: installed but still not importable: "
            f"{', '.join(still)}. Restart code-puppy and re-run /otel-setup."
        )
        return False
    emit_success(f"[1/4] deps: installed via `{detail}`")
    note = durability_note()
    if note:
        emit_warning(note)
    return True


def _walkthrough() -> bool:
    from code_puppy.messaging import (
        emit_error,
        emit_info,
        emit_success,
        emit_warning,
    )

    from . import register_callbacks as rc  # lazy: avoids import cycle

    emit_info("otel_bridge setup:")

    if not _check_deps((emit_error, emit_info, emit_success, emit_warning)):
        return True

    todo: list[str] = []

    endpoint = config.get_endpoint()
    if endpoint:
        emit_success(f"[2/4] endpoint: {endpoint}")
    else:
        emit_warning("[2/4] endpoint: unset")
        todo.append(
            "/set otel_bridge_endpoint <otlp-http-traces-url>   "
            "(e.g. http://localhost:3000/api/public/otel/v1/traces "
            "for a local Langfuse)"
        )

    headers = config.get_headers()
    if headers:
        # Key names only -- header VALUES are credentials; never echo them.
        emit_success(f"[3/4] headers: {', '.join(sorted(headers))} configured")
    else:
        emit_info(
            "[3/4] headers: none set (fine for unauthenticated backends). "
            "For HTTP Basic auth (e.g. Langfuse pk/sk): "
            "/otel-setup auth <user> <secret>"
        )

    if config.is_enabled():
        emit_success("[4/4] enabled: true")
    else:
        emit_warning("[4/4] enabled: false")
        todo.append("/set otel_bridge_enabled true")

    if todo:
        emit_info(
            "Next step(s):\n  "
            + "\n  ".join(todo)
            + "\nThen run /otel-setup again."
        )
        return True

    # Everything green: activate live. _instrument() is idempotent and
    # one-way, so calling it here instead of demanding a restart is safe.
    if not rc._INSTRUMENTED:
        rc._instrument()
    if rc._INSTRUMENTED:
        emit_success(f"otel_bridge: {rc._LAST_STATUS_REASON}")
    else:
        emit_error(
            "otel_bridge: config looks complete but instrumentation "
            f"failed -- {rc._LAST_STATUS_REASON}"
        )
    return True


@register_command(
    name="otel-setup",
    description="Install otel_bridge deps and walk through tracing config",
    usage="/otel-setup [auth <user> <secret>]",
    category="plugin",
    detailed_help=(
        "/otel-setup            checklist: installs missing OTel packages,\n"
        "                       reports config state, activates tracing\n"
        "                       live once everything is set.\n"
        "/otel-setup auth U S   writes otel_bridge_headers with an HTTP\n"
        "                       Basic Authorization header from U:S\n"
        "                       (e.g. Langfuse public/secret key pair).\n"
        "                       NOTE: typed commands persist in\n"
        "                       ~/.code_puppy/command_history.txt; the\n"
        "                       header is stored plaintext in puppy.cfg.\n"
        "Config keys involved: otel_bridge_enabled, otel_bridge_endpoint,\n"
        "otel_bridge_headers, otel_bridge_service_name (see /otel-status)."
    ),
)
def handle_otel_setup_command(command: str) -> bool:
    args = command.split()[1:]
    if args and args[0] == "auth":
        return _handle_auth(args[1:])
    return _walkthrough()
