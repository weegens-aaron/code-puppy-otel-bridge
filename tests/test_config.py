"""Pins for otel_bridge.config -- the config-key surface, nothing more.

Each test patches ``config.get_value`` directly rather than touching a
real ``puppy.cfg``, keeping these pure unit tests with no filesystem
dependency.
"""

from __future__ import annotations

import pytest

from otel_bridge import config


def _patch_get_value(monkeypatch: pytest.MonkeyPatch, values: dict[str, str | None]):
    monkeypatch.setattr(config, "get_value", lambda key: values.get(key))


# ---------------------------------------------------------------------------
# is_enabled
# ---------------------------------------------------------------------------


def test_is_enabled_defaults_false(monkeypatch):
    _patch_get_value(monkeypatch, {})
    assert config.is_enabled() is False


@pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on"])
def test_is_enabled_truthy_values(monkeypatch, raw):
    _patch_get_value(monkeypatch, {"otel_bridge_enabled": raw})
    assert config.is_enabled() is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", "nonsense"])
def test_is_enabled_falsy_values(monkeypatch, raw):
    _patch_get_value(monkeypatch, {"otel_bridge_enabled": raw})
    assert config.is_enabled() is False


# ---------------------------------------------------------------------------
# get_endpoint
# ---------------------------------------------------------------------------


def test_get_endpoint_none_when_unset(monkeypatch):
    _patch_get_value(monkeypatch, {})
    assert config.get_endpoint() is None


def test_get_endpoint_strips_whitespace(monkeypatch):
    _patch_get_value(
        monkeypatch, {"otel_bridge_endpoint": "  http://localhost:3000/x  "}
    )
    assert config.get_endpoint() == "http://localhost:3000/x"


# ---------------------------------------------------------------------------
# get_headers
# ---------------------------------------------------------------------------


def test_get_headers_empty_when_unset(monkeypatch):
    _patch_get_value(monkeypatch, {})
    assert config.get_headers() == {}


def test_get_headers_parses_comma_separated_pairs(monkeypatch):
    _patch_get_value(
        monkeypatch,
        {"otel_bridge_headers": "Authorization=Basic abc123,x-langfuse-ingestion-version=4"},
    )
    assert config.get_headers() == {
        "Authorization": "Basic abc123",
        "x-langfuse-ingestion-version": "4",
    }


def test_get_headers_skips_malformed_pairs(monkeypatch):
    _patch_get_value(
        monkeypatch, {"otel_bridge_headers": "good=value, no-equals-sign , ="}
    )
    assert config.get_headers() == {"good": "value"}


# ---------------------------------------------------------------------------
# get_service_name
# ---------------------------------------------------------------------------


def test_get_service_name_defaults(monkeypatch):
    _patch_get_value(monkeypatch, {})
    assert config.get_service_name() == "code-puppy"


def test_get_service_name_reads_config(monkeypatch):
    _patch_get_value(monkeypatch, {"otel_bridge_service_name": "my-fleet"})
    assert config.get_service_name() == "my-fleet"
