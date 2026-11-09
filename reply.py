"""Decide what to send: continue the chat as me, run the few hard checks, rewrite or hand off."""
from __future__ import annotations

import re

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
        if upset := prompts.girl_trigger(history, prompts.GIRL_UPSET):
            return Decision("handoff", reason=f"她好像不开心了：「{upset}」")
        if invite := prompts.girl_trigger(history, prompts.GIRL_INVITE):
            # She's asking about meeting or my plans: only I know my schedule, so ask me.
            return Decision("ask", reason=ask_line(target, invite))
    if media := prompts.unseen_media(history):
        # No description of the picture (vision failed): guessing goes wrong, so the user answers.
        said = " / ".join(media)
        return Decision("ask", reason=f"她发了{said}，我看不到图，怎么回？")
    if fact := prompts.fact_question(history):
        return Decision("ask", reason=ask_line(target, fact))
    return _draft(target, history)


ASK_LINE = """\
{who}在微信上跟“我”说了下面的话，我得亲自决定怎么回。把它改写成问我的一句话，
用“你”指我，格式像：她问你这周末有没有空吃饭，怎么回？
只输出这一句，别加别的，别编她没说的内容。

{who}说：{said}"""


def ask_line(target: dict, said: str) -> str:
    """Turn her words into one question for me in 文件传输助手: 「她问你……，怎么回？」"""
    who = "她" if target.get("kind") == "girl" else "他"
    try:
        line = llm.chat("你只输出一句中文。", ASK_LINE.format(who=who, said=said),
                        max_tokens=80, temperature=0.2, timeout=20).strip().splitlines()[0].strip()
    except Exception:  # noqa: BLE001 - fall back to quoting her
        line = ""
    if not line or len(line) > 80:
        line = f"{who}说「{said}」，怎么回？"
    return line


CARRY_ON = re.compile(r"^(但是)?你?(可以)?(继续|接着|自己)(对话|聊|回)?(呀|吧|啊)?[。！!]?$|^(你)?(回吧|聊吧|继续吧)$")


def compose_instructed(target: dict, history: list[dict], instruction: str) -> Decision:
    """Reply the way I said to in 文件传输助手. My word overrides every check,
    except that it never just repeats her words back."""
    if instruction.startswith("原话发这句："):
        return Decision("reply", lines=[instruction.split("：", 1)[1].strip()])  # my exact words
    if CARRY_ON.match(instruction.strip()):
        return compose(target, history)  # "go on chatting": a normal reply with every check
    feedback = ""
    for _ in range(MAX_DRAFTS):
        tail = prompts.instructed_tail(instruction) + (f"\n\n{feedback}" if feedback else "")
        draft = prompts.parse_reply(llm.chat_messages(prompts.chat_messages(target, history, tail)))
        if draft.kind != "reply" or instruction.startswith("原话发这句"):
            break
        if not (echo := prompts.parroted(draft.lines, history)):
            break
        feedback = f"上一版里「{echo}」是在重复她的原话，别学她说话，换成我自己的回应"
        draft = Decision("reply", lines=[l for l in draft.lines if l != echo]) if len(draft.lines) > 1 else draft
    if draft.kind == "reply" and draft.lines and (instruction.startswith("原话发这句")
                                                  or not prompts.parroted(draft.lines, history)):
        return draft
    if draft.kind == "reply":
        return Decision("skip", reason="写出来的都是在重复她的话")
    if instruction.startswith("你刚才替我回了："):
        return Decision("skip", reason="纠正已记住，不用补话")  # a correction is for the bot, never sent as-is
    return Decision("reply", lines=[instruction])  # model balked: send my words as-is


def compose_opener(target: dict, history: list[dict], quiet_minutes: int) -> Decision:
    """Start a new topic in a chat that has gone quiet."""
    return _draft(target, history, prompts.opener_tail(quiet_minutes))


def _draft(target: dict, history: list[dict], tail: str = "") -> Decision:
    if shape := prompts.sample_shape(target):
        target = {**target, "reply_shape": shape}  # one length per reply, kept across rewrites
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
        if draft.kind == "skip" and prompts.asked_me(history) and attempt < MAX_DRAFTS:
            reason = "对方在直接问我，不能不回"
            feedback = "对方最后一句是在直接问我，必须回答，别输出 [SKIP]。"
            continue
        if draft.kind != "reply":
            return draft
        if not target.get("faces", True):  # no WeChat faces ([旺柴]) here, emoji instead
            draft = Decision("reply", lines=[s for s in (prompts.WECHAT_FACE.sub("", l).strip()
                                                         for l in draft.lines) if s])
            if not draft.lines:
                feedback = "别只发一个表情，回一句话"
                continue

        problem = None
        if shape := target.get("reply_shape"):
            n, limit = shape
            n, limit = n + 1, limit + 8  # room for the small hook the prompt allows
            draft = Decision("reply", lines=draft.lines[:n])
            if prompts.reply_chars(draft.lines) > limit + 3:
                if attempt == MAX_DRAFTS and prompts.reply_chars(draft.lines[:1]) <= limit + 3:
                    draft = Decision("reply", lines=draft.lines[:1])  # last try: keep just the first line
                else:
                    problem = f"太长了（{prompts.reply_chars(draft.lines)} 个字），这次最多 {n} 句、不超过 {limit} 个字"
        if problem:
            pass
        elif target.get("reply_shape") and prompts.dead_end(draft.lines) and attempt < MAX_DRAFTS:
            problem = "光附和一句她没法接，后面加一句跟她刚说的事有关的小问题"
        elif again := prompts.repeats_me(draft.lines, history):
            problem = f"“{again}”和我前面说过的话几乎一样，别重复自己的梗"
        elif echo := prompts.parroted(draft.lines, history):
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
