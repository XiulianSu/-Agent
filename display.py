"""Terminal presentation layer — all ANSI output lives here."""

from __future__ import annotations

import sys
from pathlib import Path

_RESET = "\033[0m"
_CYAN = "\033[36m"
_GRAY = "\033[90m"
_MAGENTA = "\033[35m"
_YELLOW = "\033[33m"
_RED = "\033[31m"


def prompt_input() -> str:
    return input(f"{_CYAN}s08 >> {_RESET}")


def blank_line() -> None:
    print()


def show_startup(*, env_file: Path, model: str, shell_key_mismatch: bool) -> None:
    print("s08: Context Compact — four-layer compaction pipeline")
    print(f"配置来源: {env_file}")
    if shell_key_mismatch:
        print("提示：检测到终端 export 的 OPENROUTER_API_KEY 与 .env 不同，已使用 .env 中密钥。")
    print(f"当前激活模型: {model}")
    print("Type a question, press Enter. Type q to quit.\n")


def show_fatal_error(message: str) -> None:
    print(message, file=sys.stderr)


def show_install_hint(package: str) -> None:
    print(f"正在为您安装 {package} 依赖包...")


def show_auth_error() -> None:
    print("OpenRouter 认证失败 (401)。请检查 .env 里的 OPENROUTER_API_KEY。", file=sys.stderr)


def show_network_timeout() -> None:
    print(
        "连接 OpenRouter 超时（SSL/网络）。常见原因：\n"
        "  1. 终端设置了 HTTP_PROXY/HTTPS_PROXY 但代理不可用\n"
        "  2. 网络无法直连 openrouter.ai\n"
        "可尝试：unset HTTP_PROXY HTTPS_PROXY ALL_PROXY；"
        "或在 .env 加 OPENROUTER_TRUST_ENV=0 绕过系统代理。",
        file=sys.stderr,
    )


def show_assistant(content: str) -> None:
    print(content)


def show_auto_compact() -> None:
    print("[auto compact]")


def show_reactive_compact() -> None:
    print("[reactive compact]")


def show_transcript_saved(path: Path) -> None:
    print(f"[transcript saved: {path}]")


def show_subagent_spawned() -> None:
    print(f"\n{_MAGENTA}[Subagent spawned]{_RESET}")


def show_subagent_tool(name: str, output: str, preview_len: int = 100) -> None:
    preview = str(output)[:preview_len]
    print(f"  {_GRAY}[sub] {name}: {preview}{_RESET}")


def show_subagent_done() -> None:
    print(f"{_MAGENTA}[Subagent done]{_RESET}")


def show_hook_blocked(pattern: str) -> None:
    print(f"\n{_RED}⛔ Blocked: '{pattern}'{_RESET}")


def show_hook_pre_tool(tool_name: str) -> None:
    print(f"{_GRAY}[HOOK] {tool_name}{_RESET}")


def show_hook_user_prompt(workdir: Path) -> None:
    print(f"{_GRAY}[HOOK] UserPromptSubmit: working in {workdir}{_RESET}")


def show_hook_stop(tool_count: int) -> None:
    print(f"{_GRAY}[HOOK] Stop: session used {tool_count} tool calls{_RESET}")


def show_todo_list(todos: list[dict]) -> None:
    lines = [f"\n{_YELLOW}## Current Tasks{_RESET}"]
    icons = {
        "pending": " ",
        "in_progress": f"{_CYAN}▸{_RESET}",
        "completed": f"\033[32m✓{_RESET}",
    }
    for t in todos:
        icon = icons[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
