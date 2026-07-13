"""Agent session state — todos, message history, and loop counters."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentState:
    messages: list[dict] = field(default_factory=list)
    todos: list[dict] = field(default_factory=list)
    rounds_since_todo: int = 0

    def add_message(self, message: dict) -> None:
        self.messages.append(message)

    def replace_messages(self, messages: list[dict]) -> None:
        self.messages = messages

    def update_todos(self, todos: list) -> str | None:
        """Normalize and store todos. Returns error message, or None on success."""
        from tools import _normalize_todos

        normalized, error = _normalize_todos(todos)
        if error:
            return error
        self.todos = normalized
        return None

    def reset_rounds_since_todo(self) -> None:
        self.rounds_since_todo = 0

    def increment_rounds_since_todo(self) -> None:
        self.rounds_since_todo += 1

    def maybe_add_todo_reminder(self, threshold: int = 3) -> bool:
        """Append a todo nag reminder when overdue. Returns True if added."""
        if self.rounds_since_todo >= threshold and self.messages:
            self.add_message({
                "role": "user",
                "content": "<reminder>Update your todos.</reminder>",
            })
            self.reset_rounds_since_todo()
            return True
        return False
