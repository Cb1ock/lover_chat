"""The flirt judge: when she's teasing or leaves an opening, draft one cheeky line for me to
review. It never sends anything: bot.py puts the line in 文件传输助手 next to the normal reply,
and I pick ("发" = the cheeky one, "普通" = the normal one) or write my own.
"""
from __future__ import annotations

import json
import re

import llm
import prompts

JUDGE = """\
你在帮“我”看一段微信聊天。我们现在是朋友，我想慢慢往暧昧发展，她偶尔会撩我。
判断她最新的几句话是不是在撩我、逗我、给我留了可以接着调情的口子；如果是，替我写一句带点骚的回复。

关于她：{about}
记忆：
{memory}

最近的聊天（“我（本人）”是我，最新的在最后）：
{lines}

我本来准备回：{normal}

要求：
- 只在真有口子时才写（她在逗我、话里有双关、发自拍或者说自己在泳池/床上/洗澡这类、主动问我想不想她）；普通聊天就不写
- 骚是暧昧、坏、有点暗示，不是下流：不提具体身体部位和性行为，不油腻，不说“宝贝”“亲爱的”
- 像我平时说话：短（15 个字以内）、口语、可以带一个 emoji
- 不能和记忆里的事实矛盾，不编我没做过的事，不定具体见面的时间地点

只输出一行 JSON：{{"flirt": true 或 false, "line": "那句骚话", "why": "她哪句话给了口子"}}"""


def judge(target: dict, history: list[dict], normal: list[str]) -> dict | None:
    """Returns {"line", "why"} when there's an opening worth a cheeky line, else None."""
    raw = llm.chat("你只输出 JSON。", JUDGE.format(
        about=target.get("about", ""), memory=prompts.memory_text(target),
        lines=prompts.transcript(history[-20:]), normal=" / ".join(normal) or "（不回）"),
        max_tokens=1200, reasoning_effort="low", timeout=90)
    m = re.search(r"\{.*\}", raw, flags=re.S)
    try:
        verdict = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return None
    line = prompts.strip_human_mark(str(verdict.get("line", "")).strip())
    if not verdict.get("flirt") or not line:
        return None
    return {"line": line, "why": str(verdict.get("why", ""))}
