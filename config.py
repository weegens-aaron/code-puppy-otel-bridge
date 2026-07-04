"""Config helpers for otel_bridge.

Follows the same pattern as ``code_puppy.plugins.config`` and
``code_puppy.plugins.statusline.config`` -- thin wrappers around
``code_puppy.config.get_value`` / ``set_value`` (backed by ``puppy.cfg``)
so the rest of this plugin never touches the config file format
directly. All keys are namespaced ``otel_bridge_*`` to avoid colliding
with core or other plugins (see AGENTS.md coexistence rule).

Config keys
-----------
otel_bridge_enabled
    ``"true"``/``"false"`` (also accepts 1/yes/on). Default: disabled.
    Opt-in deliberately -- instrumenting every agent run and shipping
    spans over the network is a real behavior change, not something to
    switch on silently.
otel_bridge_endpoint
    Full OTLP HTTP traces endpoint URL, e.g. for a local self-hosted
    Langfuse: ``http://localhost:3000/api/public/otel/v1/traces``. Passed
    verbatim as ``OTLPSpanExporter(endpoint=...)`` -- include the full
    path, this is NOT the bare ``OTEL_EXPORTER_OTLP_ENDPOINT`` root some
    backends also accept.
otel_bridge_headers
    Extra HTTP headers for the exporter, using the same comma-separated
    ``Key1=Value1,Key2=Value2`` format as the standard
    ``OTEL_EXPORTER_OTLP_HEADERS`` env var. For Langfuse's Basic-auth
    scheme: ``Authorization=Basic <base64(public_key:secret_key)>``.
    Computing that base64 value is left to the user/setup docs -- this
    plugin does not store raw API keys itself, only the final header
    string, so it stays backend-agnostic (no Langfuse-specific key
    fields here; see AGENTS.md for why).
otel_bridge_service_name
    OTel ``service.name`` resource attribute. Default: ``"code-puppy"``.
"""

from __future__ import annotations

from code_puppy.config import get_value, set_config_value

_ENABLED_KEY = "otel_bridge_enabled"
_ENDPOINT_KEY = "otel_bridge_endpoint"
_HEADERS_KEY = "otel_bridge_headers"
_SERVICE_NAME_KEY = "otel_bridge_service_name"

_DEFAULT_SERVICE_NAME = "code-puppy"
_TRUTHY = ("1", "true", "yes", "on")

__all__ = [
    "get_endpoint",
    "get_headers",
    "get_service_name",
    "is_enabled",
    "set_headers",
]


def is_enabled() -> bool:
    """Whether the bridge should instrument on startup. Default: False."""
    val = get_value(_ENABLED_KEY)
    return str(val).strip().lower() in _TRUTHY if val else False


def get_endpoint() -> str | None:
    """The configured OTLP traces endpoint URL, or None if unset."""
    val = get_value(_ENDPOINT_KEY)
    return val.strip() if val else None


def get_headers() -> dict[str, str]:
    """Parse ``otel_bridge_headers`` into a dict for the OTLP exporter.

    Uses the standard ``OTEL_EXPORTER_OTLP_HEADERS`` comma-separated
    ``Key=Value`` format. Malformed pairs (no ``=``) are skipped rather
    than raising -- a typo in one header shouldn't crash startup.
    Returns ``{}`` if the key is unset.
    """
    raw = get_value(_HEADERS_KEY)
    if not raw:
        return {}
    headers: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key = key.strip()
        if key:
            headers[key] = value.strip()
    return headers


def get_service_name() -> str:
    """The OTel ``service.name`` resource attribute."""
    return get_value(_SERVICE_NAME_KEY) or _DEFAULT_SERVICE_NAME


def set_headers(value: str) -> None:
    """Write ``otel_bridge_headers`` (used by ``/otel-setup auth``).

    The only config WRITE this plugin performs, and only ever on
    explicit user command -- never flip ``otel_bridge_enabled`` from
    code (AGENTS.md ground rule).
    """
    set_config_value(_HEADERS_KEY, value)
