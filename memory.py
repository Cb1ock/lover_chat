"""Long-term memory per chat: facts distilled from the conversation, beyond the
HISTORY_TURNS messages the model sees directly.

data/memory_<key>.json = {"pinned": [...], "facts": [...], "covered_until": <time of last summarized message>}
"pinned" are the key facts I set by hand: always shown first, never rewritten by the summarizer.
The file is plain JSON; edit the facts by hand whenever you like.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import llm

DATA_DIR = Path(__file__).with_name("data")
EVERY = 15        # summarize once this many new messages have piled up
BATCH = 60        # at most this many messages per summarizing call
MAX_FACTS = 40

EXTRACT = """\
你在帮“我”整理一段微信聊天的长期记忆。

置顶的关键信息（只读，不用复述，也别和它矛盾）：
{pinned}

已有记忆：
{facts}

新的聊天记录（“我”就是我本人，带日期时间）：
{lines}

把新记录里值得长期记住的事合并进记忆：
- 关于对方：喜好和讨厌的东西、工作学习近况、提到的人（名字和关系）、宠物、身体状况、情绪状态
- 计划和日期：考试、面试、出行、生日等，“下周三”这类相对时间换算成具体日期（如 10月15日）
- 我们之间：称呼、共同的梗、聊过但没聊完的话题、约定（注明是否已兑现）
- 我自己在聊天里说过的关于我的事（我的立场、我提过的近况），免得以后前后矛盾
要求：
- 只记聊天里明确说过的，不推测、不脑补
- 每条写清楚是谁说的、谁做的：“我”说的话只算我的，别人说我怎样（调侃、起哄、猜测）要写成“X 调侃我……”，不能写成我承认了
- 玩笑、反话、起哄、黄段子不当成事实；看不到内容的图片只记“发了张图”，别替图片编内容
- 和已有记忆合并：重复的合并，过时的更新（比如“下周面试”变成“面试已通过”），已经过去很久且不重要的删掉
- 每条一句话，尽量具体，带日期（如果有）；最多 {max_facts} 条，重要的放前面
- 闲聊里没什么值得记的，就原样返回已有记忆
只输出一个 JSON 字符串数组，不要别的内容。"""


def path(key: str) -> Path:
    return DATA_DIR / f"memory_{key}.json"


def load(key: str) -> dict:
    p = path(key)
    if p.exists():
        mem = json.loads(p.read_text(encoding="utf-8"))
        mem.setdefault("pinned", [])
        mem.setdefault("corrections", [])
        return mem
    return {"pinned": [], "facts": [], "corrections": [], "covered_until": 0}


def add_correction(key: str, text: str) -> None:
    """I corrected a reply in 文件传输助手: keep it for every later reply in this chat."""
    mem = load(key)
    mem["corrections"] = (mem["corrections"] + [text])[-30:]
    save(key, mem)


def save(key: str, mem: dict) -> None:
    tmp = path(key).with_suffix(".tmp")
    tmp.write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path(key))  # atomic: replies read this file while it's being updated


def pending(key: str, rows: list[dict]) -> list[dict]:
    """Messages not yet folded into memory."""
    covered = load(key)["covered_until"]
    return [r for r in rows if r["time"] > covered]


def history_rows(key: str) -> list[dict]:
    p = DATA_DIR / f"history_{key}.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def catch_up(key: str, *, minimum: int = EVERY) -> int:
    """Fold pending messages into memory batch by batch while at least `minimum` are waiting."""
    folded = 0
    while len(pending(key, rows := history_rows(key))) >= minimum:
        folded += update(key, rows)
    return folded


def update(key: str, rows: list[dict]) -> int:
    """Fold up to BATCH pending messages into memory. Returns how many were folded."""
    mem = load(key)
    batch = pending(key, rows)[:BATCH]
    if not batch:
        return 0
    facts = extract(mem, batch)  # slow; pinned/corrections may be edited meanwhile
    mem = load(key)
    mem["facts"], mem["covered_until"] = facts, batch[-1]["time"]
    save(key, mem)
    return len(batch)


def extract(mem: dict, rows: list[dict]) -> list[str]:
    """Fold rows into mem["facts"] and return the new list (pure: nothing is saved)."""
    lines = "\n".join(
        f"{time.strftime('%m-%d %H:%M', time.localtime(r['time']))} {r['sender']}：{r['text']}" for r in rows
    )
    raw = llm.chat("你只输出 JSON。",
                   EXTRACT.format(pinned="\n".join(f"- {f}" for f in mem.get("pinned", [])) or "（暂无）",
                                  facts="\n".join(f"- {f}" for f in mem["facts"]) or "（暂无）",
                                  lines=lines, max_facts=MAX_FACTS),
                   max_tokens=6000, reasoning_effort="medium", timeout=240)
    m = re.search(r"\[.*\]", raw, flags=re.S)
    new_facts = json.loads(m.group(0)) if m else None
    if not isinstance(new_facts, list):
        raise ValueError(f"记忆整理结果看不懂：{raw[:200]}")
    return [str(f).strip() for f in new_facts if str(f).strip()][:MAX_FACTS]
