"""Context memory pipeline — pure functions that return new message lists."""

from __future__ import annotations

import copy
from pathlib import Path

KEEP_RECENT = 3
PERSIST_THRESHOLD = 15000
SEMANTIC_KEEP_MAX = 8
SEMANTIC_SCORE_THRESHOLD = 60

TOOL_RESULT_DIR: Path = Path.cwd() / ".task_outputs" / "tool_results"


def configure_memory(
    *,
    tool_result_dir: Path,
    keep_recent: int = KEEP_RECENT,
    persist_threshold: int = PERSIST_THRESHOLD,
    semantic_keep_max: int = SEMANTIC_KEEP_MAX,
    semantic_score_threshold: int = SEMANTIC_SCORE_THRESHOLD,
) -> None:
    global TOOL_RESULT_DIR, KEEP_RECENT, PERSIST_THRESHOLD
    global SEMANTIC_KEEP_MAX, SEMANTIC_SCORE_THRESHOLD
    TOOL_RESULT_DIR = tool_result_dir
    KEEP_RECENT = keep_recent
    PERSIST_THRESHOLD = persist_threshold
    SEMANTIC_KEEP_MAX = semantic_keep_max
    SEMANTIC_SCORE_THRESHOLD = semantic_score_threshold


def _message_has_tool_use(msg: dict) -> bool:
    if msg.get("role") != "assistant":
        return False
    return bool(msg.get("tool_calls"))


def _is_tool_result_message(msg: dict) -> bool:
    return msg.get("role") == "tool"


def _message_text(msg: dict) -> str:
    if msg.get("role") == "tool":
        return str(msg.get("content", ""))[:120]
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
        elif isinstance(block, dict):
            btype = block.get("type")
            if btype == "text":
                parts.append(str(block.get("text", "")))
            elif btype == "tool_result":
                parts.append(str(block.get("content", ""))[:120])
    return "\n".join(p for p in parts if p).strip()


def semantic_score(msg: dict, idx: int, total: int) -> int:
    text = _message_text(msg)
    if not text:
        return 0

    score = 0
    lower = text.lower()

    if "#prompt_improvement" in lower:
        score += 120
    if "#hard_constraint" in lower:
        score += 120

    keywords_hard = ["必须", "不要", "禁止", "只能", "务必", "must", "never", "do not"]
    if any(k in lower for k in keywords_hard):
        score += 40

    keywords_decision = ["结论", "决定", "采用", "方案", "最终", "确认"]
    if any(k in text for k in keywords_decision):
        score += 25

    keywords_progress = ["下一步", "todo", "待办", "计划", "分步"]
    if any(k in lower for k in keywords_progress):
        score += 15

    if msg.get("role") == "user":
        score += 10

    age = total - idx
    score -= min(age // 20, 20)
    return score


def collect_semantic_pins(messages: list[dict], start: int, end: int) -> list[tuple[int, str]]:
    picked = []
    total = len(messages)

    for i in range(start, end):
        msg = messages[i]
        s = semantic_score(msg, i, total)
        if s >= SEMANTIC_SCORE_THRESHOLD:
            text = _message_text(msg)
            if text:
                picked.append((s, text))

    picked.sort(key=lambda x: x[0], reverse=True)
    top = picked[:SEMANTIC_KEEP_MAX]

    seen = set()
    dedup = []
    for s, t in top:
        key = t.strip()
        if key in seen:
            continue
        seen.add(key)
        dedup.append((s, t))
    return dedup


def snip_compact(messages: list[dict], max_messages: int = 50) -> list[dict]:
    if len(messages) <= max_messages:
        return list(messages)

    keep_head = 3
    keep_tail = max_messages - keep_head
    head_end = keep_head
    tail_start = len(messages) - keep_tail

    if head_end > 0 and _message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_result_message(messages[head_end]):
            head_end += 1

    if (
        tail_start > 0
        and tail_start < len(messages)
        and _is_tool_result_message(messages[tail_start])
        and _message_has_tool_use(messages[tail_start - 1])
    ):
        tail_start -= 1

    if head_end >= tail_start:
        return list(messages)

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
    return messages[:head_end] + [placeholder] + messages[tail_start:]


def collect_tool_results(messages: list[dict]) -> list[tuple[int, int, dict]]:
    blocks = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "tool":
            continue
        blocks.append((mi, 0, msg))
    return blocks


def micro_compact(messages: list[dict], keep_recent: int | None = None) -> list[dict]:
    recent = KEEP_RECENT if keep_recent is None else keep_recent
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= recent:
        return list(messages)

    new_messages = copy.deepcopy(messages)
    for _, _, block in collect_tool_results(new_messages)[:-recent]:
        text = str(block.get("content", ""))
        if len(text) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return new_messages


def persist_large_output(tool_use_id: str, output: str) -> str:
    if len(output) <= PERSIST_THRESHOLD:
        return output

    TOOL_RESULT_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULT_DIR / f"{tool_use_id}.txt"
    if not path.exists():
        path.write_text(output)

    preview = output[:2000]
    return (
        f"<output_persisted>\nFull output:{path}\nPreview:\n{preview}\n</output_persisted>"
    )


def tool_result_budget(messages: list[dict], max_bytes: int = 200_000) -> list[dict]:
    if not messages:
        return []

    blocks: list[tuple[int, dict]] = []
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "tool":
            blocks.insert(0, (i, msg))
        elif blocks:
            break

    if not blocks:
        return list(messages)

    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    if total <= max_bytes:
        return list(messages)

    new_messages = copy.deepcopy(messages)
    new_blocks: list[tuple[int, dict]] = []
    for i in range(len(new_messages) - 1, -1, -1):
        msg = new_messages[i]
        if msg.get("role") == "tool":
            new_blocks.insert(0, (i, msg))
        elif new_blocks:
            break

    total = sum(len(str(b.get("content", ""))) for _, b in new_blocks)
    ranked = sorted(
        new_blocks,
        key=lambda x: len(str(x[1].get("content", ""))),
        reverse=True,
    )
    for _, block in ranked:
        if total <= max_bytes:
            break
        raw = str(block.get("content", ""))
        if len(raw) <= PERSIST_THRESHOLD:
            continue
        tid = block.get("tool_call_id", "unknown")
        block["content"] = persist_large_output(tid, raw)
        total = sum(len(str(b.get("content", ""))) for _, b in new_blocks)
    return new_messages


def apply_compaction_pipeline(messages: list[dict]) -> list[dict]:
    """Run the three-layer in-memory compaction pipeline (pure)."""
    msgs = tool_result_budget(messages)
    msgs = snip_compact(msgs)
    return micro_compact(msgs)


# Re-export for compact_history / reactive_compact in the main program
message_has_tool_use = _message_has_tool_use
is_tool_result_message = _is_tool_result_message
