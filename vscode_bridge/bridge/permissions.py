"""Coding permission policy for edits and terminal commands."""

from __future__ import annotations

import os
import re

_CATASTROPHIC = [
    re.compile(
        r"(^|\s)rm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)\s+"
        r"(/|~|\.|\*|\.\.|\$HOME|\$\{HOME\})(\s|$)",
        re.IGNORECASE,
    ),
    re.compile(r"(^|\s)mkfs(\s|$)", re.IGNORECASE),
    re.compile(r"(^|\s)dd\s+if=", re.IGNORECASE),
    re.compile(r"(^|\s)shutdown(\s|$)", re.IGNORECASE),
    re.compile(r"(^|\s)reboot(\s|$)", re.IGNORECASE),
    re.compile(r"(^|\s)format(\s|$)", re.IGNORECASE),
    re.compile(r"(^|\s)del\s+/[sq]", re.IGNORECASE),
]

_RISKY = [
    re.compile(r"(^|\s)(rm|rmdir|del|mv|cp|chmod|chown|kill|pkill|sudo|su)(\s|$)", re.IGNORECASE),
    re.compile(r"(^|\s)(curl|wget|ssh|scp|rsync|nc|netcat)(\s|$)", re.IGNORECASE),
    re.compile(r"git\s+push(\s|$)", re.IGNORECASE),
    re.compile(r"git\s+reset\s+--hard", re.IGNORECASE),
    re.compile(r"git\s+clean\s+-[a-z]*f", re.IGNORECASE),
    re.compile(r"(npm|pnpm|yarn)\s+(install|i|add|remove|uninstall|exec|dlx)(\s|$)", re.IGNORECASE),
    re.compile(r"(pip|pip3|uv)\s+(install|uninstall|sync)(\s|$)", re.IGNORECASE),
    re.compile(r"(docker|kubectl|systemctl|service)(\s|$)", re.IGNORECASE),
]

_SAFE_SUBCOMMANDS = {
    "npm": {"test", "t"},
    "pnpm": {"test", "t"},
    "yarn": {"test", "t"},
}


def is_catastrophic_command(command: str, args: list[str] | None = None) -> bool:
    joined = f"{command} {' '.join(args or [])}".strip()
    return any(pattern.search(joined) for pattern in _CATASTROPHIC)


def is_risky_command(command: str, args: list[str] | None = None) -> bool:
    args = [str(item) for item in (args or [])]
    if is_catastrophic_command(command, args):
        return True
    joined = f"{command} {' '.join(args)}".strip()
    if any(pattern.search(joined) for pattern in _RISKY):
        return True

    executable = os.path.basename(command).lower()
    if executable == "pytest":
        return False
    if executable in {"python", "python3"}:
        return not (len(args) >= 2 and args[0] == "-m" and args[1] in {"pytest", "unittest"})
    if executable in {"npm", "pnpm", "yarn"}:
        if not args:
            return True
        if args[0] in _SAFE_SUBCOMMANDS[executable]:
            return False
        if len(args) >= 2 and args[0] == "run" and args[1] in {
            "test", "build", "lint", "typecheck", "check", "format",
        }:
            return False
        return True
    if executable in {"npx", "pnpx", "yarnx"}:
        return True
    if executable == "cargo":
        return not (args and args[0] in {"test", "check", "build", "clippy", "fmt"})
    if executable == "go":
        return not (args and args[0] in {"test", "build", "vet"})
    if executable == "dotnet":
        return not (args and args[0] in {"test", "build"})
    if executable in {"mvn", "mvnw", "gradle", "gradlew"}:
        return not (args and args[0] in {"test", "verify", "build", "check"})
    if executable == "make":
        return not (args and args[0] in {"test", "check", "lint", "build", "verify"})
    if executable == "git":
        return not (args and args[0] in {
            "status", "diff", "log", "show", "branch", "rev-parse", "describe",
        })
    if executable in {"rg", "grep", "find", "ls", "pwd", "whoami", "head", "tail", "wc"}:
        return False
    return True


def command_requires_approval(
    permission_mode: str,
    command: str,
    args: list[str] | None = None,
) -> bool:
    if permission_mode == "full_access":
        return False
    if permission_mode == "ask":
        return True
    return is_risky_command(command, args)
