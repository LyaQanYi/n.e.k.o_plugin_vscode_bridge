from __future__ import annotations

from vscode_bridge.bridge.protocol import PROTOCOL_VERSION, BridgeError, parse_message
from vscode_bridge.bridge.settings import BridgeSettings


def test_protocol_version_is_positive() -> None:
    assert PROTOCOL_VERSION >= 1


def test_parse_message_rejects_non_object() -> None:
    try:
        parse_message("[]")
    except BridgeError as exc:
        assert exc.code == "INTERNAL_ERROR"
    else:
        raise AssertionError("expected BridgeError")


def test_default_settings_are_valid() -> None:
    settings = BridgeSettings()
    assert settings.port == 45821
    assert settings.model_mode == "auto"
    assert settings.permission_mode == "ask"
    assert settings.allow_terminal is True
