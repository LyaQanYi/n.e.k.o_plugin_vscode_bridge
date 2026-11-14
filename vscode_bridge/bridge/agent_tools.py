"""OpenAI function-calling schemas for the dedicated coding agent."""

from __future__ import annotations

import copy
from typing import Any


def _tool(name: str, description: str, properties: dict[str, Any] | None = None,
          required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }


TOOL_SPECS: list[dict[str, Any]] = [
    _tool(
        "workspace_info",
        "Return workspace roots, name, trust state, and VSCode version.",
    ),
    _tool(
        "list_files",
        "List files in the workspace. Prefer this before reading unknown areas.",
        {
            "glob": {"type": "string", "description": "Glob such as '**/*.ts'."},
            "exclude": {"type": "array", "items": {"type": "string"}},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20000},
        },
    ),
    _tool(
        "read_file",
        "Read a text file, optionally a line window. Returns a sha256 base_hash.",
        {
            "path": {"type": "string", "description": "Workspace-relative or absolute path inside a root."},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "max_chars": {"type": "integer", "minimum": 1, "maximum": 2000000},
        },
        ["path"],
    ),
    _tool(
        "search_text",
        "Search file contents. Use regex=false for a literal search.",
        {
            "query": {"type": "string"},
            "regex": {"type": "boolean"},
            "include": {"type": "string", "description": "Glob such as 'src/**/*.ts'."},
            "exclude": {"type": "array", "items": {"type": "string"}},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
            "context_lines": {"type": "integer", "minimum": 0, "maximum": 5},
        },
        ["query"],
    ),
    _tool(
        "active_editor",
        "Return the active editor file, selection, language, and visible text.",
    ),
    _tool(
        "get_diagnostics",
        "Return compiler/linter diagnostics for the workspace or one file.",
        {
            "path": {"type": "string"},
            "severity": {"type": "string", "enum": ["error", "warning", "info", "hint", "all"]},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
        },
    ),
    _tool(
        "apply_edits",
        "Propose exact text replacements in a file. Read the file first and pass its base_hash.",
        {
            "path": {"type": "string"},
            "base_hash": {"type": "string", "description": "sha256 returned by read_file."},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                    },
                    "required": ["old_text", "new_text"],
                    "additionalProperties": False,
                },
                "minItems": 1,
            },
            "summary": {"type": "string", "description": "Short user-facing summary."},
        },
        ["path", "base_hash", "edits", "summary"],
    ),
    _tool(
        "create_file",
        "Propose creating a new text file.",
        {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "summary": {"type": "string"},
        },
        ["path", "content", "summary"],
    ),
    _tool(
        "delete_file",
        "Propose deleting a file. Read it first and pass its base_hash.",
        {
            "path": {"type": "string"},
            "base_hash": {"type": "string"},
            "summary": {"type": "string"},
        },
        ["path", "base_hash", "summary"],
    ),
    _tool(
        "rename_file",
        "Propose renaming or moving a file.",
        {
            "from_path": {"type": "string"},
            "to_path": {"type": "string"},
            "summary": {"type": "string"},
        },
        ["from_path", "to_path", "summary"],
    ),
    _tool(
        "run_command",
        "Run a test/build command after the user approves it. Never use a shell pipeline.",
        {
            "command": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
            "cwd": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
            "reason": {"type": "string"},
        },
        ["command", "reason"],
    ),
    _tool(
        "finish_task",
        "Finish the task and return a concise summary.",
        {
            "summary": {"type": "string"},
            "files_changed": {"type": "array", "items": {"type": "string"}},
            "tests": {"type": "string"},
            "remaining_issues": {"type": "string"},
        },
        ["summary"],
    ),
]


def get_tool_specs() -> list[dict[str, Any]]:
    return copy.deepcopy(TOOL_SPECS)


TOOL_NAMES = {item["function"]["name"] for item in TOOL_SPECS}
