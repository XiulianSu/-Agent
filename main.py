#!/usr/bin/env python3
"""
s07: Skill Loading — two-level on-demand knowledge injection.

  Layer 1 (cheap, always present):
    SYSTEM prompt includes skill names + one-line descriptions (~100 tokens/skill)
    "Skills available: agent-builder, code-review, mcp-builder, pdf"

  Layer 2 (expensive, on demand):
    Agent calls load_skill("code-review") → full SKILL.md content
    injected via tool_result (~2000 tokens/skill)

  skills/
    agent-builder/SKILL.md
    code-review/SKILL.md
    mcp-builder/SKILL.md
    pdf/SKILL.md

Changes from s06:
  + build_system() — scan skills/ dir at startup, inject catalog into SYSTEM
  + load_skill(name) — return full SKILL.md content via tool_result
  + SKILLS_DIR config
  Loop unchanged: load_skill auto-dispatches via TOOL_HANDLERS.

Run: python S08_learn_claude_code_自己写版本.py
Needs: pip install openai python-dotenv pyyaml + OPENROUTER_API_KEY in .env
"""

import json, os, subprocess, sys
from pathlib import Path
import yaml
import time

from display import (
    blank_line,
    prompt_input,
    show_assistant,
    show_auth_error,
    show_auto_compact,
    show_fatal_error,
    show_hook_blocked,
    show_hook_pre_tool,
    show_hook_stop,
    show_hook_user_prompt,
    show_install_hint,
    show_network_timeout,
    show_reactive_compact,
    show_startup,
    show_subagent_done,
    show_subagent_spawned,
    show_subagent_tool,
    show_transcript_saved,
)
from memory import (
    apply_compaction_pipeline,
    configure_memory,
    is_tool_result_message,
    message_has_tool_use,
)
from state import AgentState
from tools import (
    create_tool_registry,
    parse_tool_args,
    run_tool,
    set_workdir,
    SUB_HANDLERS,
    SUB_TOOLS,
    TOOLS,
)

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

try:
    from openai import OpenAI
    from openai import APITimeoutError, AuthenticationError
except ImportError:
    show_install_hint("openai")
    subprocess.run("pip install openai", shell=True, check=False)
    from openai import OpenAI, APITimeoutError, AuthenticationError

try:
    from dotenv import load_dotenv
except ImportError:
    show_install_hint("python-dotenv")
    subprocess.run("pip install python-dotenv", shell=True, check=False)
    from dotenv import load_dotenv

# 从脚本同目录 .env 加载（覆盖终端 export，和 S07 保持一致）
_ENV_FILE = Path(__file__).resolve().parent / ".env"
_shell_key_before = os.environ.get("OPENROUTER_API_KEY")
load_dotenv(_ENV_FILE, override=True)

WORKDIR = Path.cwd()
set_workdir(WORKDIR)
SKILLS_DIR = WORKDIR / "skills"

TRANSCRIPT_DIR = WORKDIR / ".transcripts" # 在当前工作目录下创建一个".transcripts"目录，用于存储对话记录
TOOL_RESULT_DIR = WORKDIR / ".task_outputs" / "tool_results" # 在当前工作目录下创建一个".task_outputs"目录，用于存储工具调用结果

CONTEXT_LIMIT = 50000
MAX_REACTIVE_RETRIES = 1

configure_memory(tool_result_dir=TOOL_RESULT_DIR)

OPENROUTER_API_KEY = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
if not OPENROUTER_API_KEY:
    show_fatal_error(
        f"错误：未设置 OPENROUTER_API_KEY。\n"
        f"请检查 {_ENV_FILE} 并填入密钥。"
    )
    sys.exit(1)

MODEL = os.environ.get("OPENROUTER_MODEL", "moonshotai/kimi-k2.6")
OPENROUTER_TIMEOUT = float(os.environ.get("OPENROUTER_TIMEOUT", "120"))
OPENROUTER_TRUST_ENV = os.environ.get("OPENROUTER_TRUST_ENV", "1").strip().lower() not in (
    "0", "false", "no",
)

try:
    import httpx
    _http_client = httpx.Client(
        timeout=OPENROUTER_TIMEOUT,
        trust_env=OPENROUTER_TRUST_ENV,
    )
except ImportError:
    _http_client = None

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
    timeout=OPENROUTER_TIMEOUT,
    **({"http_client": _http_client} if _http_client else {}),
)
# s07: Skill catalog scan (used by build_system below)
def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from SKILL.md. Returns (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2].strip()

# Build skill registry at startup (used for safe lookup in load_skill)
SKILL_REGISTRY: dict[str, dict] = {}

def _scan_skills():
    """Scan skills/ dir, populate SKILL_REGISTRY with name/description/content."""
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text()
            meta, body = _parse_frontmatter(raw)
            name = meta.get("name", d.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}

_scan_skills()

def list_skills() -> str:
    """List all skills (name + one-line description)."""
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values())

# s07: SYSTEM includes skill catalog (cheap — just names + descriptions)
def build_system() -> str:
    """Build SYSTEM prompt with skill catalog injected at startup."""
    catalog = list_skills()
    return (
        f"You are a coding agent at {WORKDIR}. "
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed."
    )

SYSTEM = build_system()

# s07: subagent gets its own system prompt — no skill loading, no task
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


def extract_text(content) -> str:
    if not isinstance(content, list): # 如果content不是列表，则返回content的字符串表示
        return str(content) # 将content转换为字符串
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")# 将content中的文本块拼接成一个字符串

def assistant_message_to_dict(message) -> dict:
    msg: dict = {
        "role": "assistant",
        "content": message.content if message.content is not None else "",
    }
    if message.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": tc.type or "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "{}",
                },
            }
            for tc in message.tool_calls
        ]
    return msg

def estimate_size(msgs):
    return len(str(msgs))

# 在对话前把完整对话存到盘里面，避免摘要后丢历史
def write_transcript(messages):

    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True) 
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl" #一行写一条 json 记录，并且记录时间戳
    with path.open("w") as f: # 以写入模式打开 path 这个模式，没有就新建，有就覆盖，令文件对象叫 f
        for msg in messages: # 对于循环变量里面的每一个消息；msg 的意思是每一条消息
            f.write(json.dumps(msg,default=str) + "\n") # 把 msg 转换成 json 格式，然后写入文件
    return path

# 调用LLM 把长对话压缩成短的摘要
def summarize_history(messages):
    # 把整个列表变成 JSON文本字符串，遇到无法转化的，用 str()转换成字符串，只取前面80000个字符
    conversation = json.dumps(messages,default=str)[:80000] 
    prompt = ("Summarize this coding-agent conversation so work can continue.\n"
              "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
              "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n" + conversation
    )
    # 调用模型，来生成摘要
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
    )
    summary = (response.choices[0].message.content or "").strip()
    if not summary:
        return "(empty summary)"
    return summary

def compact_history(messages):
    transcript_path = write_transcript(messages)
    show_transcript_saved(transcript_path)
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]

def reactive_compact(messages):
    write_transcript(messages)
    tail_start = max(0, len(messages) - 5)
    if (tail_start > 0
        and is_tool_result_message(messages[tail_start])
        and message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    summary = summarize_history(messages[:tail_start])
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"},
            *messages[tail_start:]]
# ═══════════════════════════════════════════════════════════
#  FROM s06 (unchanged): Subagent
# ═══════════════════════════════════════════════════════════

def spawn_subagent(description: str) -> str:
    show_subagent_spawned()
    sub_messages = [{"role": "user", "content": description}]
    final_text = ""

    for _ in range(30):
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SUB_SYSTEM}] + sub_messages,
            tools=SUB_TOOLS,
            tool_choice="auto",
        )
        response_message = response.choices[0].message
        sub_messages.append(assistant_message_to_dict(response_message))

        tool_calls = response_message.tool_calls
        if not tool_calls:
            final_text = (response_message.content or "").strip()
            break

        for tool_call in tool_calls:
            name = tool_call.function.name
            args = parse_tool_args(tool_call.function.arguments)

            blocked = trigger_hooks("PreToolUse", name, args)
            if blocked:
                sub_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": name,
                    "content": str(blocked),
                })
                continue

            output = run_tool(name, args, SUB_HANDLERS)
            trigger_hooks("PostToolUse", name, args, output)
            show_subagent_tool(name, output)
            sub_messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": name,
                "content": output,
            })

    if not final_text:
        for msg in reversed(sub_messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                final_text = str(msg["content"]).strip()
                if final_text:
                    break
    if not final_text:
        final_text = "Subagent stopped after 30 turns without final answer."
    show_subagent_done()
    return final_text


# ═══════════════════════════════════════════════════════════
#  NEW in s07: load_skill — runtime full content loading
# ═══════════════════════════════════════════════════════════

def load_skill(name: str) -> str:
    """Load full skill content. Lookup via registry — no path traversal."""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]


TOOL_HANDLERS = create_tool_registry(spawn_subagent, load_skill)


# ═══════════════════════════════════════════════════════════
#  FROM s04 (unchanged): Hook System
# ═══════════════════════════════════════════════════════════

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None

DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]

def permission_hook(tool_name: str, args: dict):
    if tool_name == "bash":
        for p in DENY_LIST:
            if p in args.get("command", ""):
                show_hook_blocked(p)
                return "Permission denied"
    return None

def log_hook(tool_name: str, args: dict):
    show_hook_pre_tool(tool_name)
    return None

def context_inject_hook(query: str):
    show_hook_user_prompt(WORKDIR)
    return None

def summary_hook(messages: list):
    tool_count = sum(1 for m in messages if m.get("role") == "tool")
    show_hook_stop(tool_count)
    return None

register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — orchestration only; display via display.py
# ═══════════════════════════════════════════════════════════

def _compact_context(state: AgentState) -> None:
    state.replace_messages(apply_compaction_pipeline(state.messages))
    if estimate_size(state.messages) > CONTEXT_LIMIT:
        show_auto_compact()
        state.replace_messages(compact_history(state.messages))


def _call_model(state: AgentState):
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": SYSTEM}] + state.messages,
        tools=TOOLS,
        tool_choice="auto",
    )
    response_message = response.choices[0].message
    state.add_message(assistant_message_to_dict(response_message))
    return response_message


def _try_reactive_compact(state: AgentState, exc: Exception, retries: int) -> bool:
    msg = str(exc).lower()
    if ("prompt_too_long" not in msg and "too many tokens" not in msg) or retries >= MAX_REACTIVE_RETRIES:
        return False
    show_reactive_compact()
    state.replace_messages(reactive_compact(state.messages))
    return True


def _finish_if_no_tools(state: AgentState, response_message) -> bool:
    """Return True when the turn is complete (no further tool calls)."""
    if response_message.tool_calls:
        return False
    force = trigger_hooks("Stop", state.messages)
    if force:
        state.add_message({"role": "user", "content": force})
        return False
    if response_message.content:
        show_assistant(response_message.content)
    return True


def _execute_tool_calls(state: AgentState, tool_calls) -> None:
    state.increment_rounds_since_todo()
    for tool_call in tool_calls:
        name = tool_call.function.name
        args = parse_tool_args(tool_call.function.arguments)

        blocked = trigger_hooks("PreToolUse", name, args)
        if blocked:
            state.add_message({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": name,
                "content": str(blocked),
            })
            continue

        output = run_tool(name, args, TOOL_HANDLERS)
        trigger_hooks("PostToolUse", name, args, output)

        if name == "todo_write" and not output.startswith("Error"):
            state.update_todos(args["todos"])
            state.reset_rounds_since_todo()

        state.add_message({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "name": name,
            "content": output,
        })


def agent_loop(state: AgentState) -> None:
    reactive_retries = 0
    while True:
        state.maybe_add_todo_reminder()
        _compact_context(state)
        try:
            response_message = _call_model(state)
            reactive_retries = 0
        except AuthenticationError:
            show_auth_error()
            raise
        except APITimeoutError:
            show_network_timeout()
            return
        except Exception as e:
            if not _try_reactive_compact(state, e, reactive_retries):
                raise
            reactive_retries += 1
            continue
        if _finish_if_no_tools(state, response_message):
            return
        _execute_tool_calls(state, response_message.tool_calls)


if __name__ == "__main__":
    show_startup(
        env_file=_ENV_FILE,
        model=MODEL,
        shell_key_mismatch=bool(
            _shell_key_before and _shell_key_before.strip() != OPENROUTER_API_KEY
        ),
    )

    state = AgentState()
    while True:
        try:
            query = prompt_input()
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        trigger_hooks("UserPromptSubmit", query)
        state.add_message({"role": "user", "content": query})
        agent_loop(state)
        blank_line()