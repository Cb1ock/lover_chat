"""The invitation judge: a separate model call that decides whether now is a good moment to
ask her out, at what weight, and drafts the line. It only advises; bot.py acts on it.

- "soft": no date, place or promise ("下次去你那约饭") -> may go out with the reply
- "concrete": a day, a place, a plan -> sent to me in 文件传输助手 first; I'm the one who'd show up

data/invites_<key>.json remembers when we last invited, per girl.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import llm
import prompts

DATA_DIR = Path(__file__).with_name("data")

# How bold and how often, per relationship stage. Days = minimum gap between invitations.
STAGE_POLICY = {
    "刚认识": ("很少邀约。只有聊得很投机、她自己提到想做某件事时，才给一次轻邀约；不给具体邀约。", 7),
    "暧昧": ("她流露兴趣（分享想吃想去的、说无聊、周末没安排、聊得很热）时可以约。轻邀约可以常有，"
             "具体邀约在气氛好、她有空的时候提。", 3),
    "恋爱": ("可以大方约，具体邀约为主；她累了、不开心时改成关心，不约。", 1),
    "升温": ("现在是朋友，我想慢慢往暧昧发展，异地。她聊得开心、提到喜欢的吃的玩的、说想出去玩时，"
             "给一次轻松的轻邀约（“来我这边请你吃”“下次一起去玩”）；她说要来我的城市或我要去她那时，再给具体邀约。", 4),
    "朋友": ("她已经说过只当朋友。只在她自己提到相关的事（想吃某家、要来我这个城市、说以后可以约饭）时，"
             "给一次朋友式的轻邀约；不给单独、暧昧的具体邀约。", 7),
}

JUDGE = """\
你是一个旁观的恋爱顾问，帮“我”判断：现在要不要在微信上约她。你不替我聊天，只给判断。

我们现在的阶段：{stage}
这个阶段的分寸：{policy}
关于她和我们：{about}
关键信息和记忆：
{memory}
距离上次邀约：{since}

最近的聊天（“我（本人）”就是我，最新的在最后）：
{lines}

判断：
1. 现在适合约吗？要有自然的由头（她提到想吃/想去/想做的事，聊得正热，她有空），她累了、不开心、敷衍、刚拒绝过就不约
2. 约的话，是“soft”（不定时间地点，比如“下次去你那请你吃这家”）还是“concrete”（具体到哪天、去哪、做什么）
3. 写出一句我会发的邀约，口语、短、像我平时说话，别油腻；要和关键信息一致（比如异地）

只输出一行 JSON：{{"invite": true 或 false, "kind": "soft 或 concrete", "line": "邀约的话", "reason": "一句话理由"}}"""


def path(key: str) -> Path:
    return DATA_DIR / f"invites_{key}.json"


def last_invite(key: str) -> float:
    p = path(key)
    return json.loads(p.read_text())["last"] if p.exists() else 0.0


def mark_invited(key: str, line: str) -> None:
    p = path(key)
    log = json.loads(p.read_text()) if p.exists() else {"last": 0, "sent": []}
    log["last"] = time.time()
    log["sent"].append({"time": int(time.time()), "line": line})
    p.write_text(json.dumps(log, ensure_ascii=False, indent=2))


def due(target: dict, key: str) -> bool:
    """Cheap pre-check before spending a model call: her chat, a known stage, cooldown passed."""
    if target.get("kind") != "girl" or not target.get("invite", True):
        return False
    _, days = STAGE_POLICY.get(target.get("stage", "暧昧"), STAGE_POLICY["暧昧"])
    return time.time() - last_invite(key) >= days * 86400


def judge(target: dict, key: str, history: list[dict]) -> dict | None:
    """Ask the judge. Returns {"kind", "line", "reason"} when it says invite, else None."""
    stage = target.get("stage", "暧昧")
    policy, _ = STAGE_POLICY.get(stage, STAGE_POLICY["暧昧"])
    last = last_invite(key)
    since = f"{(time.time() - last) / 86400:.0f} 天" if last else "还没约过"
    raw = llm.chat("你只输出 JSON。", JUDGE.format(
        stage=stage, policy=policy, about=target.get("about", ""), memory=prompts.memory_text(target),
        since=since, lines=prompts.transcript(history[-30:])), max_tokens=1500, reasoning_effort="low", timeout=120)
    m = re.search(r"\{.*\}", raw, flags=re.S)
    try:
        verdict = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return None
    if not verdict.get("invite") or not str(verdict.get("line", "")).strip():
        return None
    kind = "concrete" if verdict.get("kind") == "concrete" else "soft"
    return {"kind": kind, "line": prompts.strip_human_mark(str(verdict["line"]).strip()),
            "reason": str(verdict.get("reason", ""))}
