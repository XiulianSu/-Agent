"""Tool implementations and registry for the S08 coding agent."""

import ast
import json
import subprocess
from pathlib import Path
from typing import Callable

WORKDIR: Path = Path.cwd()


def set_workdir(path: Path) -> None:
    global WORKDIR
    WORKDIR = path


def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    try:
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def _normalize_todos(todos):
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None


def run_todo_write(todos: list) -> str:
    from display import show_todo_list

    todos, error = _normalize_todos(todos)
    if error:
        return error
    show_todo_list(todos)
    return f"Updated {len(todos)} tasks"


def _openai_tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def parse_tool_args(arguments: str) -> dict:
    try:
        return json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {}


BASE_TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "todo_write": run_todo_write,
}

SUB_TOOLS = [
    _openai_tool("bash", "Run a shell command.",
        {"command": {"type": "string"}}, ["command"]),
    _openai_tool("read_file", "Read file contents.",
        {"path": {"type": "string"}}, ["path"]),
    _openai_tool("write_file", "Write content to a file.",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _openai_tool("edit_file", "Replace exact text in a file once.",
        {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
        ["path", "old_text", "new_text"]),
    _openai_tool("glob", "Find files matching a glob pattern.",
        {"pattern": {"type": "string"}}, ["pattern"]),
]

SUB_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}

TOOLS = [
    _openai_tool("bash", "Run a shell command.",
        {"command": {"type": "string"}}, ["command"]),
    _openai_tool("read_file", "Read file contents.",
        {"path": {"type": "string"}, "limit": {"type": "integer"}}, ["path"]),
    _openai_tool("write_file", "Write content to a file.",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _openai_tool("edit_file", "Replace exact text in a file once.",
        {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
        ["path", "old_text", "new_text"]),
    _openai_tool("glob", "Find files matching a glob pattern.",
        {"pattern": {"type": "string"}}, ["pattern"]),
    _openai_tool("todo_write", "Create and manage a task list for your current coding session.",
        {"todos": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
            },
            "required": ["content", "status"],
        }}}, ["todos"]),
    _openai_tool("task", "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
        {"description": {"type": "string"}}, ["description"]),
    _openai_tool("load_skill", "Load the full content of a skill by name.",
        {"name": {"type": "string"}}, ["name"]),
]


def run_tool(name: str, args: dict, handlers: dict) -> str:
    handler = handlers.get(name)
    if not handler:
        return f"Unknown: {name}"
    try:
        return handler(**args)
    except TypeError as e:
        return f"Error: bad arguments for {name}: {e}"


def create_tool_registry(
    subagent_func: Callable[..., str],
    load_skill_func: Callable[..., str],
) -> dict[str, Callable]:
    """Assemble full TOOL_HANDLERS from base tools + main-program callbacks."""
    return {
        **BASE_TOOL_HANDLERS,
        "task": subagent_func,
        "load_skill": load_skill_func,
    }
