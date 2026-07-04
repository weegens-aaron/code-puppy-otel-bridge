"""One-shot: write otel_bridge config into puppy.cfg via the host API.

Usage: python scripts/configure_puppy.py <BASIC_AUTH_B64> [OTLP_ENDPOINT]

``BASIC_AUTH_B64`` is ``base64(<public-key>:<secret-key>)`` for your
backend (e.g. a Langfuse project's pk:sk). ``OTLP_ENDPOINT`` defaults to
a local Langfuse stack's OTLP/HTTP traces endpoint.

Idempotent -- just overwrites the four otel_bridge_* keys. Run again if
the auth string or endpoint ever changes.
"""

from __future__ import annotations

import sys

from code_puppy.config import get_value, set_config_value

DEFAULT_ENDPOINT = "http://localhost:3000/api/public/otel/v1/traces"

if len(sys.argv) not in (2, 3):
    print("usage: python scripts/configure_puppy.py <BASIC_AUTH_B64> [OTLP_ENDPOINT]")
    sys.exit(2)

endpoint = sys.argv[2].strip() if len(sys.argv) == 3 else DEFAULT_ENDPOINT

set_config_value("otel_bridge_enabled", "true")
set_config_value("otel_bridge_endpoint", endpoint)
set_config_value("otel_bridge_headers", f"Authorization=Basic {sys.argv[1].strip()}")
set_config_value("otel_bridge_service_name", "code-puppy")

for key in (
    "otel_bridge_enabled",
    "otel_bridge_endpoint",
    "otel_bridge_headers",
    "otel_bridge_service_name",
):
    val = get_value(key)
    # Never echo credentials -- header values land in scrollback/CI logs.
    shown = "<set, value hidden>" if key == "otel_bridge_headers" and val else val
    print(f"{key} = {shown}")
print("DONE -- restart code-puppy to activate instrumentation.")
