from __future__ import annotations

from vscode_bridge.bridge.permissions import (
    command_requires_approval,
    is_catastrophic_command,
    is_risky_command,
)


def test_ask_always_requires_approval() -> None:
    assert command_requires_approval("ask", "pytest", []) is True
    assert command_requires_approval("ask", "npm", ["test"]) is True
    assert command_requires_approval("ask", "git", ["push"]) is True


def test_auto_edit_allows_safe_commands() -> None:
    assert command_requires_approval("auto_edit", "pytest", ["-q"]) is False
    assert command_requires_approval("auto_edit", "npm", ["test"]) is False
    assert command_requires_approval("auto_edit", "npm", ["run", "build"]) is False
    assert command_requires_approval("auto_edit", "cargo", ["test"]) is False
    assert command_requires_approval("auto_edit", "git", ["status"]) is False


def test_auto_edit_asks_for_risky_commands() -> None:
    assert command_requires_approval("auto_edit", "git", ["push"]) is True
    assert command_requires_approval("auto_edit", "rm", ["-rf", "dist"]) is True
    assert command_requires_approval("auto_edit", "npm", ["install"]) is True
    assert command_requires_approval("auto_edit", "curl", ["https://example.com"]) is True


def test_full_access_skips_approval() -> None:
    assert command_requires_approval("full_access", "git", ["push"]) is False
    assert command_requires_approval("full_access", "rm", ["-rf", "dist"]) is False
    assert command_requires_approval("full_access", "pytest", ["-q"]) is False


def test_catastrophic_commands_are_detected() -> None:
    assert is_catastrophic_command("rm", ["-rf", "/"]) is True
    assert is_catastrophic_command("rm", ["-rf", "~"]) is True
    assert is_catastrophic_command("rm", ["-rf", "dist"]) is False
    assert is_catastrophic_command("mkfs", ["/dev/sda"]) is True


def test_risky_classifier_keeps_safe_tests_safe() -> None:
    assert is_risky_command("pytest", []) is False
    assert is_risky_command("python", ["-m", "pytest"]) is False
    assert is_risky_command("python", ["script.py"]) is True
    assert is_risky_command("git", ["push"]) is True
