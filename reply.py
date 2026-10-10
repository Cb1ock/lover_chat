"""Decide what to send: generate a draft, have it reviewed, rewrite or hand off."""
from __future__ import annotations

import llm
import prompts
from prompts import Decision

MAX_DRAFTS = 3


def compose(target: dict, history: list[dict]) -> Decision:
    """Answer the latest messages."""
    known = target.get("agent_known", False)
    doubt = None if known else prompts.suspicion(history)
    if doubt:
        return Decision("handoff", reason=f"对方好像起疑了：「{doubt}」")
    if target["kind"] == "girl":
        hers = prompts.girl_handoff(history)
        if hers:
            return Decision("handoff", reason=f"她在约你或者不开心了：「{hers}」")
    return _draft_and_review(target, history, prompts.user_prompt(history), ai_talk_ok=known)


def compose_instructed(target: dict, history: list[dict], instruction: str) -> Decision:
    """Reply the way I said to in 文件传输助手. My word overrides every check."""
    draft = prompts.parse_reply(llm.chat(prompts.system_prompt(target),
                                         prompts.instructed_prompt(history, instruction)))
    if draft.kind == "reply":
        return draft
    return Decision("reply", lines=[instruction])  # model balked: send my words as-is


def compose_opener(target: dict, history: list[dict], quiet_minutes: int) -> Decision:
    """Start a new topic in a chat that has gone quiet."""
    return _draft_and_review(target, history, prompts.opener_prompt(history, quiet_minutes))


def _review(target: dict, history: list[dict], lines: list[str]) -> tuple[str, str]:
    """Ask the reviewer; an unreadable verdict gets one retry before it counts as a handoff."""
    for _ in range(2):
        raw = llm.chat("你是严格的审核员，只输出 JSON。",
                       prompts.review_prompt(target, history, lines),
                       max_tokens=2000, reasoning_effort="low")
        verdict, reason = prompts.parse_review(raw)
        if reason != prompts.UNREADABLE_REVIEW:
            return verdict, reason
    return verdict, reason


def _draft_and_review(target: dict, history: list[dict], user: str, *, ai_talk_ok: bool = False) -> Decision:
    """ai_talk_ok: replying in a chat that knows about the agent, so teasing about it is fine."""
    system = prompts.system_prompt(target)
    ask = user
    reason = ""
    feedback: list[str] = []  # every rejected draft so far, so rewrites don't ping-pong
    for attempt in range(1, MAX_DRAFTS + 1):
        draft = prompts.parse_reply(llm.chat(system, ask))
        if draft.kind != "reply":
            return draft
        promise = prompts.commitment(draft.lines)
        bot_talk = None if ai_talk_ok else prompts.ai_topic(draft.lines)
        tell = prompts.ai_tell(draft.lines)
        echo = prompts.parroted(draft.lines, history)
        # Repetition is a style nit: on the last try, a repeated word beats saying nothing.
        repeat = prompts.repeated_phrase(draft.lines, history) if attempt < MAX_DRAFTS else None
        if echo:
            verdict, reason = "rewrite", f"“{echo}”是在复读对方的话，用我自己的话回"
        elif promise:
            verdict, reason = "rewrite", f"“{promise}”是在替我承诺或安排"
        elif bot_talk:
            verdict, reason = "rewrite", f"提到了“{bot_talk}”：不许聊 AI/机器人/自动回复，换个别的话题"
        elif tell:
            verdict, reason = "rewrite", f"“{tell}”AI 味太重，像真人一样随口说"
        elif repeat:
            verdict, reason = "rewrite", f"“{repeat}”我前面刚说过，别重复这个梗，换个说法或换个角度"
        else:
            verdict, reason = _review(target, history, draft.lines)
        if verdict == "ok":
            return draft
        if verdict == "handoff":
            return Decision("handoff", reason=reason)
        feedback.append(prompts.rewrite_suffix(draft.lines, reason))
        ask = user + "".join(feedback)
    # Only the reviewer's "handoff" needs me; failing style checks just means stay quiet.
    return Decision("skip", reason=f"改了 {MAX_DRAFTS} 次还是不合适，这次不回：{reason}")
