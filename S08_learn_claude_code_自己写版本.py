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

import ast, json, os, subprocess, sys
from pathlib import Path
import yaml
import time

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

try:
    from openai import OpenAI
    from openai import AuthenticationError
except ImportError:
    print("正在为您安装 openai 依赖包...")
    subprocess.run("pip install openai", shell=True, check=False)
    from openai import OpenAI, AuthenticationError

try:
    from dotenv import load_dotenv
except ImportError:
    print("正在为您安装 python-dotenv 依赖包...")
    subprocess.run("pip install python-dotenv", shell=True, check=False)
    from dotenv import load_dotenv

# 从脚本同目录 .env 加载（覆盖终端 export，和 S07 保持一致）
_ENV_FILE = Path(__file__).resolve().parent / ".env"
_shell_key_before = os.environ.get("OPENROUTER_API_KEY")
load_dotenv(_ENV_FILE, override=True)

WORKDIR = Path.cwd()
SKILLS_DIR = WORKDIR / "skills"

TRANSCRIPT_DIR = WORKDIR / ".transcripts" # 在当前工作目录下创建一个".transcripts"目录，用于存储对话记录
TOOL_RESULT_DIR = WORKDIR / ".task_outputs" / "tool_results" # 在当前工作目录下创建一个".task_outputs"目录，用于存储工具调用结果

CONTEXT_LIMIT = 50000
KEEP_RECENT = 3
PERSIST_THRESHOLD = 15000
MAX_REACTIVE_RETRIES = 1
# ═══════════════════════════════════════════════════════════
# 语义重要性的全局变量，自己加的
SEMANTIC_KEEP_MAX = 8
SEMANTIC_SCORE_THRESHOLD = 60
# ═══════════════════════════════════════════════════════════

OPENROUTER_API_KEY = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
if not OPENROUTER_API_KEY:
    print(
        f"错误：未设置 OPENROUTER_API_KEY。\n"
        f"请检查 {_ENV_FILE} 并填入密钥。",
        file=sys.stderr,
    )
    sys.exit(1)

MODEL = os.environ.get("OPENROUTER_MODEL", "moonshotai/kimi-k2.6")
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)
CURRENT_TODOS: list[dict] = []

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


# ═══════════════════════════════════════════════════════════
#  FROM s02-s06 (unchanged): Tool Implementations
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
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
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"

def extract_text(content) -> str:
    if not isinstance(content, list): # 如果content不是列表，则返回content的字符串表示
        return str(content) # 将content转换为字符串
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")# 将content中的文本块拼接成一个字符串

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

# ═══════════════════════════════════════════════════════════
#  New s08: Compress the Code
# ═══════════════════════════════════════════════════════════

def estimate_size(msgs):
    return len(str(msgs))

def _block_type(block):
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None) # 如果block是字典，则返回 block 的type属性，否则返回 block 的type属性

def _message_has_tool_use(msg):
    if msg.get("role") != "assistant": # assistant + tool_use 是模型输出：“它需要工具”
        return False
    # OpenAI 格式：assistant 消息带 tool_calls
    return bool(msg.get("tool_calls"))

def _is_tool_result_message(msg): # user+ tool_result 是程序把它的输出结果交给模型
    # OpenAI 格式：role=tool 的独立消息
    return msg.get("role") == "tool"

# ================================
# 语义重要性筛选的函数，目标是在中间被snip_compact裁剪前，把重要的 prompt / 语义提取出来保留
def _message_text(msg) -> str:
    if msg.get("role") == "tool":
        return str(msg.get("content", ""))[:120]
    content = msg.get("content")
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        # SDK block object
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
        # dict block
        elif isinstance(block, dict):
            btype = block.get("type")
            if btype == "text":
                parts.append(str(block.get("text", "")))
            elif btype == "tool_result":
                # tool_result 只取短预览，避免把大块内容再带回
                parts.append(str(block.get("content", ""))[:120])
    return "\n".join(p for p in parts if p).strip()


def semantic_score(msg, idx: int, total: int) -> int:
    text = _message_text(msg)
    if not text:
        return 0

    score = 0
    lower = text.lower()

    # 显式标签（最高优先）
    if "#prompt_improvement" in lower:
        score += 120
    if "#hard_constraint" in lower:
        score += 120

    # 约束类语义
    keywords_hard = ["必须", "不要", "禁止", "只能", "务必", "must", "never", "do not"]
    if any(k in lower for k in keywords_hard):
        score += 40

    # 决策/结论类语义
    keywords_decision = ["结论", "决定", "采用", "方案", "最终", "确认"]
    if any(k in text for k in keywords_decision):
        score += 25

    # 任务推进类语义
    keywords_progress = ["下一步", "todo", "待办", "计划", "分步"]
    if any(k in lower for k in keywords_progress):
        score += 15

    # user 消息默认更重要（你的需求演进通常在 user）
    if msg.get("role") == "user":
        score += 10

    # 轻微时间衰减（越旧略降）
    age = total - idx
    score -= min(age // 20, 20)

    return score


def collect_semantic_pins(messages, start: int, end: int):
    picked = []
    total = len(messages)

    for i in range(start, end):
        msg = messages[i]
        s = semantic_score(msg, i, total)
        if s >= SEMANTIC_SCORE_THRESHOLD:
            text = _message_text(msg)
            if text:
                picked.append((s, text))

    # 高分优先，最多保留 SEMANTIC_KEEP_MAX 条
    picked.sort(key=lambda x: x[0], reverse=True)
    top = picked[:SEMANTIC_KEEP_MAX]

    # 去重（按文本）
    seen = set()
    dedup = []
    for s, t in top:
        key = t.strip()
        if key in seen:
            continue
        seen.add(key)
        dedup.append((s, t))
    return dedup
# ================================

def snip_compact(messages, max_messages=50):
    if len(messages) <= max_messages:
        return messages
    
    keep_head = 3 # 头部保留几条
    keep_tail = max_messages - keep_head #尾部保留几条
    head_end = keep_head # 切分的位置，在下面会被 tool_result 的保护逻辑往后
    tail_start = len(messages) - keep_tail # 实际尾部的切分，在下面会被 tool_use 的保护逻辑往前推

    if head_end > 0  and _message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_result_message(messages[head_end]): # 只要还没有到结尾，并且下一个消息是tool_result的话，那戒断的位置就要往后延 1 个消息
            head_end += 1

    if (
        tail_start > 0 
        and tail_start < len(messages)
        and _is_tool_result_message(messages[tail_start])
        and _message_has_tool_use(messages[tail_start - 1])
    ):
        tail_start -= 1

    if head_end >= tail_start:
        return messages
    
    # ================================
    # 专门针对重要的语义进行筛选和保留，避免在中间被snip_compact裁剪掉，自己添加的

    snipped = tail_start - head_end
    pins = collect_semantic_pins(messages, head_end, tail_start)
    pin_block = None
    if pins:
        lines = ["[semantic_pins kept during snip]"]
        for s, t in pins:
            one_line = " ".join(t.split())[:220]
            lines.append(f"- (score={s}) {one_line}")
        pin_block = {"role": "user", "content": "\n".join(lines)}

    placeholder = {
        "role": "user",
        "content": f"[snipped {snipped} messages from conversation middle]",
    }

    if pin_block:
        return messages[:head_end] + [pin_block, placeholder] + messages[tail_start:]
    
    # ================================

    return messages[:head_end] + [placeholder] + messages[tail_start:]

def collect_tool_results(messages): # 找到 user 消息消息，且其中的content是 list，筛选出 content 中 block 的 type 为 tool_result 的，记录下它的mi,bi,block
    blocks = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "tool":
            continue
        blocks.append((mi, 0, msg))
    return blocks

def micro_compact(messages): # 解决“历史老结果太多”
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT:
        return messages
    for _, _, block in tool_results[:-KEEP_RECENT]:
        text = str(block.get("content",""))
        if len(text) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages

def persist_large_output(tool_use_id,output):
    if len(output) <=PERSIST_THRESHOLD: 
        return output

    TOOL_RESULT_DIR.mkdir(parents = True, exist_ok = True) #准备存储的目录
    path = TOOL_RESULT_DIR / f"{tool_use_id}.txt" #生成文件的路径
    if not path.exists(): 
        path.write_text(output) #如果这个文件不存在的时候，写入内容；如果存在的话，就不重复写内容
    
    preview = output[:2000]
    return f"<output_persisted>\nFull output:{path}\nPreview:\n{preview}\n</output_persisted>" #告诉模型，完整输出在磁盘里，上下文里面只放预览

def tool_result_budget(messages, max_bytes=200_000):
    if not messages:
        return messages

    # OpenAI：扫描末尾连续的 role=tool 消息
    blocks = []
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "tool":
            blocks.insert(0, (i, msg))
        elif blocks:
            break

    if not blocks:
        return messages

    total = sum(len(str(b.get("content", ""))) for _, b in blocks)

    if total <= max_bytes:
        return messages

    ranked = sorted(
        blocks,
        key=lambda x: len(str(x[1].get("content", ""))), # lambda的意思是定义一个临时小函数，输入的值是x，返回的是排序值
        reverse=True, # True 倒序，从大到小
    )
    for _, block in ranked:

        if total <= max_bytes:
            break
        
        raw = str(block.get("content", ""))
        if len(raw) <= PERSIST_THRESHOLD:
            continue
        tid = block.get("tool_call_id", "unknown")
        
        block["content"] = persist_large_output(tid, raw)
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    return messages
        
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
    print(f"[transcript saved: {transcript_path}]")
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}] # 返回只有一条消息的列表，内容是摘要，替换之前过多的上下文

def reactive_compact(messages):
    write_transcript(messages)
    tail_start = max(0, len(messages) - 5)
    if (tail_start >0
        and _is_tool_result_message(messages[tail_start])
        and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1 
    summary = summarize_history(messages[:tail_start])
    return[{"role":"user", "content":f"[Reactive compact]\n\n{summary}"},
        *messages[tail_start:]]
# ═══════════════════════════════════════════════════════════
#  FROM s06 (unchanged): Subagent
# ═══════════════════════════════════════════════════════════

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
SUB_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write,
                "edit_file": run_edit, "glob": run_glob}

def spawn_subagent(description: str) -> str:
    print(f"\n\033[35m[Subagent spawned]\033[0m")
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
            print(f"  \033[90m[sub] {name}: {str(output)[:100]}\033[0m")
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
    print(f"\033[35m[Subagent done]\033[0m")
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


# ═══════════════════════════════════════════════════════════
#  Tool Registry — all tools from s02-s07
# ═══════════════════════════════════════════════════════════

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
    # s07: skill tool (catalog is already in SYSTEM prompt, this loads full content)
    _openai_tool("load_skill", "Load the full content of a skill by name.",
        {"name": {"type": "string"}}, ["name"]),
]

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
    "task": spawn_subagent, "load_skill": load_skill,
}

def run_tool(name: str, args: dict, handlers: dict | None = None) -> str:
    active_handlers = handlers or TOOL_HANDLERS
    handler = active_handlers.get(name)
    if not handler:
        return f"Unknown: {name}"
    try:
        return handler(**args)
    except TypeError as e:
        return f"Error: bad arguments for {name}: {e}"


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
                print(f"\n\033[31m⛔ Blocked: '{p}'\033[0m")
                return "Permission denied"
    return None

def log_hook(tool_name: str, args: dict):
    print(f"\033[90m[HOOK] {tool_name}\033[0m")
    return None

def context_inject_hook(query: str):
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

def summary_hook(messages: list):
    tool_count = sum(1 for m in messages if m.get("role") == "tool")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None

register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — same as s05-s06 + nag reminder
# ═══════════════════════════════════════════════════════════

rounds_since_todo = 0

def agent_loop(messages: list):
    reactive_retries = 0
    global rounds_since_todo # 全局变量，记录to-do的次数

    while True: # to do reminder 判断是不是太久没有更新任务了
        if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0
        
        # 对上下文进行压缩，四层压缩
        messages[:] = tool_result_budget(messages)
        messages[:] = snip_compact(messages)
        messages[:] = micro_compact(messages)

        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact_history(messages)
        
        try:
        # 调用模型，在message后面添加 assistant 的消息
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": SYSTEM}] + messages,
                tools=TOOLS,
                tool_choice="auto",
            )
            response_message = response.choices[0].message
            messages.append(assistant_message_to_dict(response_message))
            reactive_retries = 0

        except AuthenticationError:
            print(
                "OpenRouter 认证失败 (401)。请检查 .env 里的 OPENROUTER_API_KEY。",
                file=sys.stderr,
            )
            raise
        except Exception as e:
            if ("prompt_too_long" in str(e).lower() or  "too many tokens" in str(e).lower())and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                messages[:] = reactive_compact(messages)
                reactive_retries += 1
                continue
            raise
        
        tool_calls = response_message.tool_calls
        # 判断是不是tool_use，如果是的话就继续循环
        if not tool_calls:
            force = trigger_hooks("Stop", messages) # 判断是否涉及到危险指令，需要强制停止
            if force:
                messages.append({"role": "user", "content": force})
                continue
            if response_message.content:
                print(response_message.content)
            return

        rounds_since_todo += 1

        for tool_call in tool_calls:
            name = tool_call.function.name
            args = parse_tool_args(tool_call.function.arguments)

            blocked = trigger_hooks("PreToolUse", name, args)
            if blocked:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": name,
                    "content": str(blocked),
                })
                continue

            output = run_tool(name, args)
            trigger_hooks("PostToolUse", name, args, output)

            if name == "todo_write":
                rounds_since_todo = 0

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": name,
                "content": output,
            })


if __name__ == "__main__":
    print("s08: Context Compact — four-layer compaction pipeline")
    print(f"配置来源: {_ENV_FILE}")
    if _shell_key_before and _shell_key_before.strip() != OPENROUTER_API_KEY:
        print("提示：检测到终端 export 的 OPENROUTER_API_KEY 与 .env 不同，已使用 .env 中密钥。")
    print(f"当前激活模型: {MODEL}")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []
    while True:

        try:
            query = input("\033[36ms08 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        print()