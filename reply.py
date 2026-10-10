"""Decide what to send: continue the chat as me, run the few hard checks, rewrite or hand off."""
from __future__ import annotations

import llm
import prompts
from prompts import Decision

MAX_DRAFTS = 3


def compose(target: dict, history: list[dict]) -> Decision:
    """Answer the latest messages."""
    known = target.get("agent_known", False)
    guarded = target.get("guardrails", True)
    doubt = prompts.suspicion(history) if guarded and not known else None
    if doubt:
        return Decision("handoff", reason=f"对方好像起疑了：「{doubt}」")
    if guarded and target["kind"] == "girl":
        hers = prompts.girl_handoff(history)
        if hers:
            return Decision("handoff", reason=f"她在约你或者不开心了：「{hers}」")
    if fact := prompts.fact_question(history):
        return Decision("ask", reason=f"{fact}（关于你本人的事，我不知道怎么答）")
    return _draft(target, history)


def compose_instructed(target: dict, history: list[dict], instruction: str) -> Decision:
    """Reply the way I said to in 文件传输助手. My word overrides every check."""
    draft = prompts.parse_reply(llm.chat_messages(
        prompts.chat_messages(target, history, prompts.instructed_tail(instruction))))
    if draft.kind == "reply":
        return draft
    return Decision("reply", lines=[instruction])  # model balked: send my words as-is


def compose_opener(target: dict, history: list[dict], quiet_minutes: int) -> Decision:
    """Start a new topic in a chat that has gone quiet."""
    return _draft(target, history, prompts.opener_tail(quiet_minutes))


def _draft(target: dict, history: list[dict], tail: str = "") -> Decision:
    guarded = target.get("guardrails", True)
    known = target.get("agent_known", False)
    feedback = ""
    reason = ""
    for attempt in range(1, MAX_DRAFTS + 1):
        note = "\n\n".join(x for x in (tail, feedback) if x)
        draft = prompts.parse_reply(llm.chat_messages(prompts.chat_messages(target, history, note)))
        if draft.kind == "handoff" and not guarded:
            # No fences in this chat: the model getting cold feet just means staying quiet.
            return Decision("skip", reason=f"模型不想接：{draft.reason}")
        if draft.kind == "ask" and prompts.ASK_NOT_A_FACT.search(draft.reason):
            return Decision("skip", reason=f"模型拿不准怎么回：{draft.reason}")
        if draft.kind != "reply":
            return draft

        problem = None
        if echo := prompts.parroted(draft.lines, history):
            problem = f"“{echo}”是在复读对方的话"
        elif not known and (bot_talk := prompts.ai_topic(draft.lines)):
            problem = f"提到了“{bot_talk}”，别聊 AI/机器人这类话题"
        elif tell := prompts.ai_tell(draft.lines):
            problem = f"“{tell}”太像 AI 了"
        elif guarded and (promise := prompts.commitment(draft.lines)):
            problem = f"“{promise}”是在替我承诺或安排"
        if problem is None and guarded:
            # The reviewer is slow and cautious: only for chats that keep the fences (hers).
            verdict, reason = _review(target, history, draft.lines)
            if verdict == "ok":
                return draft
            if verdict == "handoff":
                return Decision("handoff", reason=reason)
            problem = reason
        if problem is None:
            return draft
        reason = problem
        feedback = prompts.rewrite_tail(draft.lines, problem)
    return Decision("skip", reason=f"改了 {MAX_DRAFTS} 次还是不合适，这次不回：{reason}")


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
