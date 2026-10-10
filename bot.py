"""Auto-reply to selected WeChat chats as me, through web WeChat (wx2.qq.com).

Run:   .venv/bin/python bot.py
The first run opens a Chrome window: scan the QR code with your phone. The login is
kept in .wx_profile/ for later runs.
Pause: create a file named PAUSE in this folder (delete it to resume). Quit: Ctrl+C.
If you type in a target chat yourself, the bot stays quiet there for a while; create a
file named RESUME to end that early.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import Page, async_playwright

import config
import memory
import reply

ROOT = Path(__file__).parent
PROFILE_DIR = ROOT / ".wx_profile"
DATA_DIR = ROOT / "data"
PAUSE_FILE = ROOT / "PAUSE"
RESUME_FILE = ROOT / "RESUME"  # touch to lift every pause (takeover/handoff) right away
CATCH_UP_SECONDS = 30 * 60  # after login/RESUME, answer a chat whose last message is theirs and this recent
REPLAY_SLACK_SECONDS = 30  # our own sends are logged with local time, the replay with server time
ENGAGED_SECONDS = 600  # in mention-only groups, messages this soon after I spoke go to the model
ASK_WAIT_SECONDS = 10 * 60  # after asking me something in 文件传输助手, wait this long for my answer
RESUMED_AT_FILE = DATA_DIR / "resumed_at"  # a RESUME outlives restarts
OPENER_RETRY_SECONDS = 30 * 60  # after an opener attempt (sent or skipped), wait this long
# 文件传输助手 is my control channel: the bot posts handoff notes there, and what I type
# there afterwards is an instruction for how to reply in that chat.
FILEHELPER_LOG = DATA_DIR / "history_filehelper.jsonl"
DONE_FILE = DATA_DIR / "instructions_done.json"
PAUSES_FILE = DATA_DIR / "pauses.json"  # handoff pauses outlive restarts
# {chat key: time}: a chat I reset. Anything older is ignored, including the replay
# web WeChat sends after every login, so the chat really starts from scratch.
FORGET_FILE = DATA_DIR / "forget.json"
NOTE_PREFIX = "【lover_chat】"
INSTRUCTION_MAX_AGE = 30 * 60  # older instructions (e.g. replayed after a long downtime) are ignored
NOTE_ORDER_SLACK_SECONDS = 5  # notes carry local time, my messages server time
# A WeChat quote-reply as web WeChat renders it: "<sender>: <quoted>" / a dashed line / my words.
QUOTE_RE = re.compile(r'^"(.+?)"\s*\n\s*(?:-\s*){3,}\n(.*)$', re.S)
NOTE_RE = re.compile(re.escape(NOTE_PREFIX) + r"(.+?) (?:需要你接手|想问你)")
ASK_RE = re.compile(r"想问你：(.*)")
HOOK_JS = (ROOT / "hook.js").read_text(encoding="utf-8")
WX_URL = "https://wx2.qq.com/"
FILEHELPER = "filehelper"


@dataclass
class Chat:
    key: str
    cfg: dict
    peer: str = ""  # web WeChat UserName; changes on every login, so learned from traffic
    history: list[dict] = field(default_factory=list)
    task: asyncio.Task | None = None
    sending: bool = False
    instructing: bool = False  # carrying out my 文件传输助手 instruction: don't cancel it
    dirty: bool = False  # a new message arrived while we were sending or instructing
    paused_until: float = 0.0
    # (is it me, text) -> times, over the whole history file: replays after login can
    # reach back further than the last HISTORY_TURNS messages.
    seen_rows: dict[tuple[bool, str], list[int]] = field(default_factory=dict)
    sent_at: list[float] = field(default_factory=list)
    opener_at: list[float] = field(default_factory=list)  # when the bot started a topic
    last_open_try: float = 0.0
    memory_task: asyncio.Task | None = None
    forget_before: float = 0.0  # set when I reset this chat; older messages are ignored


page: Page
chats: dict[str, Chat] = {}
seen_ids: set[str] = set()
started_at = time.time()
logged_in = False
filehelper_rows: list[dict] = []  # my notes and instructions in 文件传输助手
done_instructions: set[str] = set()


def log(text: str) -> None:
    print(time.strftime("%H:%M:%S"), text, flush=True)


def history_file(chat: Chat) -> Path:
    return DATA_DIR / f"history_{chat.key}.jsonl"


def load_history(chat: Chat) -> None:
    path = history_file(chat)
    if path.exists():
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        chat.history = rows[-config.HISTORY_TURNS:]
        chat.opener_at = [r["time"] for r in rows if r.get("opener")]
        for r in rows:
            chat.seen_rows.setdefault((r["sender"] == "我", r["text"]), []).append(r["time"])
    # Keep honoring a takeover across restarts: did I type here myself recently?
    mine = [h for h in chat.history if h["sender"] == "我" and not h.get("auto")]
    if mine and mine[-1]["time"] > resumed_at() and takeover_seconds(chat):
        chat.paused_until = mine[-1]["time"] + takeover_seconds(chat)


def takeover_seconds(chat: Chat) -> int:
    return chat.cfg.get("takeover_seconds", config.HUMAN_TAKEOVER_SECONDS)


def load_control_state() -> None:
    if FILEHELPER_LOG.exists():
        filehelper_rows.extend(
            json.loads(l) for l in FILEHELPER_LOG.read_text(encoding="utf-8").splitlines() if l.strip()
        )
    if DONE_FILE.exists():
        done_instructions.update(json.loads(DONE_FILE.read_text(encoding="utf-8")))
    if PAUSES_FILE.exists():
        for key, until in json.loads(PAUSES_FILE.read_text()).items():
            if key in chats:
                chats[key].paused_until = max(chats[key].paused_until, until)
    if FORGET_FILE.exists():
        for key, since in json.loads(FORGET_FILE.read_text()).items():
            if key in chats:
                chats[key].forget_before = since


def save_pauses() -> None:
    now = time.time()
    PAUSES_FILE.write_text(json.dumps({c.key: c.paused_until for c in chats.values() if c.paused_until > now}))


def log_filehelper(text: str, ts: float) -> None:
    if any(r["text"] == text and abs(r["time"] - ts) <= REPLAY_SLACK_SECONDS for r in filehelper_rows):
        return
    row = {"text": text, "time": int(ts)}
    filehelper_rows.append(row)
    with FILEHELPER_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def resumed_at() -> float:
    """When I last created RESUME; anything before it, I've already seen."""
    return float(RESUMED_AT_FILE.read_text()) if RESUMED_AT_FILE.exists() else 0.0


def record(chat: Chat, sender: str, text: str, ts: float, *,
           auto: bool = False, opener: bool = False) -> None:
    row = {"sender": sender, "text": text, "time": int(ts)}
    if auto:
        row["auto"] = True  # sent by the bot, not typed by me
    if opener:
        row["opener"] = True  # the bot started this topic unprompted
    chat.history = (chat.history + [row])[-config.HISTORY_TURNS:]
    chat.seen_rows.setdefault((sender == "我", text), []).append(int(ts))
    with history_file(chat).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def cfg_for(chat: Chat) -> dict:
    """The chat's config plus its long-term memory, as the prompts expect it."""
    return {**chat.cfg, "memory": memory.load(chat.key)["facts"]}


def remember(chat: Chat) -> None:
    """Fold piled-up messages into long-term memory in the background."""
    if chat.memory_task and not chat.memory_task.done():
        return

    async def run() -> None:
        try:
            folded = await asyncio.to_thread(memory.catch_up, chat.key)
            if folded:
                log(f"[{chat.key}] 整理了 {folded} 条聊天进长期记忆")
        except Exception as e:  # noqa: BLE001 - memory is best-effort
            log(f"[{chat.key}] 整理长期记忆失败：{e}")

    chat.memory_task = asyncio.create_task(run())


def match_chat(msg: dict) -> Chat | None:
    for chat in chats.values():
        m = chat.cfg["match"]
        if "remark" in m and not msg["isGroup"] and msg["peerRemark"] == m["remark"]:
            return chat
        if "group" in m and msg["isGroup"] and m["group"] in msg["peerNick"]:
            return chat
    return None


def paused(chat: Chat) -> bool:
    return PAUSE_FILE.exists() or time.time() < chat.paused_until


def already_recorded(chat: Chat, sender: str, text: str, ts: float) -> bool:
    """Web WeChat replays recent messages after every login; don't take them as new.

    Only "me vs. them" is compared, not the name: right after login group members
    aren't loaded yet, so a replayed group message may come back as "群友".
    """
    times = chat.seen_rows.get((sender == "我", text), [])
    return any(abs(t - ts) <= REPLAY_SLACK_SECONDS for t in times)


def mentions_me(text: str) -> bool:
    """An @ of my name, a quote-reply to one of my messages ("<my name>: ..."), or a CALL_NAMES nickname."""
    return (any(f"@{n}" in text or text.startswith(f'"{n}:') for n in config.MY_NAMES)
            or any(n in text for n in config.CALL_NAMES))


def engaged(chat: Chat) -> bool:
    """I (or the bot) spoke here moments ago, so what follows may well be for me."""
    mine = [h for h in chat.history if h["sender"] == "我"]
    return bool(mine) and time.time() - mine[-1]["time"] < ENGAGED_SECONDS


def might_be_for_me(chat: Chat, msg: dict) -> bool:
    return msg.get("mentionsMe") or mentions_me(msg["text"]) or engaged(chat)


def catch_up() -> None:
    """Answer chats whose last message is theirs, recent, and still unanswered."""
    for chat in chats.values():
        last = chat.history[-1] if chat.history else None
        if not (chat.peer and last) or paused(chat) or last["sender"] == "我":
            continue
        if time.time() - last["time"] > CATCH_UP_SECONDS:
            continue
        if chat.cfg.get("only_when_mentioned") and not mentions_me(last["text"]):
            continue
        schedule_reply(chat)


async def catch_up_after_login() -> None:
    await asyncio.sleep(5)  # let the post-login replay land first
    for chat in chats.values():
        if not chat.peer:  # no replayed traffic for this chat: look it up in the contact list
            chat.peer = await page.evaluate("m => window.__lcFind(m)", chat.cfg["match"])
        remember(chat)
    catch_up()


def resume_all() -> None:
    RESUME_FILE.unlink(missing_ok=True)
    RESUMED_AT_FILE.write_text(str(time.time()))
    log("收到 RESUME，所有对话恢复自动回复")
    for chat in chats.values():
        chat.paused_until = 0.0
    save_pauses()
    catch_up()


def typing_delay(line: str) -> float:
    return min(1.0 + 0.35 * len(line) + random.uniform(0, 1.5), 8.0)


async def send(peer: str, text: str) -> dict:
    return await page.evaluate("([p, t]) => window.__lcSend(p, t)", [peer, text])


async def hand_off(chat: Chat, reason: str) -> None:
    chat.paused_until = time.time() + config.HANDOFF_PAUSE_SECONDS
    save_pauses()
    last = chat.history[-1]["text"] if chat.history else ""
    note = (f"{NOTE_PREFIX}{chat.cfg['name']} 需要你接手：{reason}\n最后一条：{last}\n"
            f"直接在这里告诉我怎么回，我照着回")
    log(f"[{chat.key}] 转交给你：{reason}")
    log_filehelper(note, time.time())
    await send(FILEHELPER, note)


def chat_of_note(note: str) -> Chat | None:
    """The chat a note was about: "【lover_chat】<name> 需要你接手：…" or "… 想问你：…"."""
    m = NOTE_RE.match(note)
    return next((c for c in chats.values() if m and c.cfg["name"] == m.group(1)), None)


def with_question(note: str, answer: str) -> str:
    """If the note asked me something, hand the model the question along with my answer."""
    m = ASK_RE.search(note)
    return f"你之前问我：{m.group(1)}\n我的回答：{answer}" if m else answer


def instruction_target(text: str, ts: float) -> tuple[Chat | None, str]:
    """Which chat an instruction is for, and the instruction itself.

    Best: a quote-reply to one of my handoff notes, which pins the chat exactly. Then a
    "对话名：" prefix. Else the latest handoff note.
    """
    quote = QUOTE_RE.match(text)
    if quote:
        quoted, instruction = quote.group(1), quote.group(2).strip()
        quoted = re.sub(r"^[^:：]{1,30}[:：]\s*", "", quoted)  # drop the "<sender>: " of the quote
        if quoted.startswith(NOTE_PREFIX) and (chat := chat_of_note(quoted)):
            return chat, with_question(quoted, instruction)
        text = instruction
    for chat in chats.values():
        for name in (chat.cfg["name"], chat.key):
            for sep in ("：", ":"):
                if text.startswith(name + sep):
                    return chat, text[len(name) + len(sep):].strip()
    notes = [r for r in filehelper_rows
             if r["text"].startswith(NOTE_PREFIX) and r["time"] <= ts + NOTE_ORDER_SLACK_SECONDS]
    if notes and (chat := chat_of_note(notes[-1]["text"])):
        return chat, with_question(notes[-1]["text"], text)
    return None, text


async def on_filehelper(msg: dict) -> None:
    text = msg["text"]
    log_filehelper(text, msg["time"])
    if not msg["fromMe"] or text.startswith(NOTE_PREFIX):
        return
    key = f"{int(msg['time'])}|{text}"
    if key in done_instructions or time.time() - msg["time"] > INSTRUCTION_MAX_AGE:
        return
    if msg["time"] < started_at:
        # Replayed after login: let the earlier handoff notes land first, then resolve.
        await asyncio.sleep(5)
        if key in done_instructions:
            return
    chat, instruction = instruction_target(text, msg["time"])
    if chat is None:
        log(f"[文件传输助手] 收到“{text}”，但不知道是给哪个对话的（开头写「对话名：」）")
        return
    done_instructions.add(key)
    DONE_FILE.write_text(json.dumps(sorted(done_instructions), ensure_ascii=False), encoding="utf-8")
    chat.paused_until = 0.0
    save_pauses()
    log(f"[{chat.key}] 收到你的指示：{instruction}")
    if chat.task and not chat.task.done():
        chat.task.cancel()
    chat.task = asyncio.create_task(instructed_flow(chat, instruction))


async def instructed_flow(chat: Chat, instruction: str) -> None:
    chat.instructing = True
    try:
        await asyncio.sleep(random.uniform(2, 5))
        if not chat.peer:
            chat.peer = await page.evaluate("m => window.__lcFind(m)", chat.cfg["match"])
        try:
            decision = await asyncio.to_thread(reply.compose_instructed, cfg_for(chat), chat.history, instruction)
        except Exception as e:  # noqa: BLE001 - keep the bot alive on any API failure
            log(f"[{chat.key}] 模型调用失败：{e}")
            return
        await send_lines(chat, decision.lines)
    finally:
        chat.instructing = False
        if chat.dirty:  # they wrote while I was being obeyed: answer that next
            chat.dirty = False
            schedule_reply(chat)


async def reply_flow(chat: Chat) -> None:
    await asyncio.sleep(random.uniform(*config.READ_DELAY))
    if paused(chat):
        return
    hour_ago = time.time() - 3600
    chat.sent_at = [t for t in chat.sent_at if t > hour_ago]
    if len(chat.sent_at) >= config.MAX_LINES_PER_HOUR:
        if chat.cfg.get("guardrails", True):
            await hand_off(chat, f"一小时内已自动回复 {len(chat.sent_at)} 条，先停一下")
        else:
            log(f"[{chat.key}] 一小时内已自动回复 {len(chat.sent_at)} 条，这条先不回")
        return

    try:
        decision = await asyncio.to_thread(reply.compose, cfg_for(chat), chat.history)
    except Exception as e:  # noqa: BLE001 - keep the bot alive on any API failure
        log(f"[{chat.key}] 模型调用失败：{e}")
        return
    if decision.kind == "skip":
        log(f"[{chat.key}] 不回复" + (f"：{decision.reason}" if decision.reason else ""))
        return
    if decision.kind == "handoff":
        await hand_off(chat, decision.reason)
        return
    if decision.kind == "ask":
        await ask_me(chat, decision.reason)
        return
    await send_lines(chat, decision.lines)


async def ask_me(chat: Chat, question: str) -> None:
    """The bot needs a fact only I know: ask in 文件传输助手 and hold the chat until I answer."""
    chat.paused_until = time.time() + ASK_WAIT_SECONDS
    save_pauses()
    last = chat.history[-1]["text"] if chat.history else ""
    note = (f"{NOTE_PREFIX}{chat.cfg['name']} 想问你：{question}\n最后一条：{last}\n"
            f"在这里回答（最好引用这条），我照着回")
    log(f"[{chat.key}] 问你：{question}")
    log_filehelper(note, time.time())
    await send(FILEHELPER, note)


async def send_lines(chat: Chat, lines: list[str], *, opener: bool = False) -> None:
    chat.sending = True
    try:
        for line in lines:
            await asyncio.sleep(typing_delay(line))
            if paused(chat):
                break
            res = await send(chat.peer, line)
            if not res["ok"]:
                log(f"[{chat.key}] 发送失败：{line}")
                break
            seen_ids.add(res["id"])
            record(chat, "我", line, time.time(), auto=True, opener=opener)
            chat.sent_at.append(time.time())
            log(f"[{chat.key}] 我（{'主动' if opener else '自动'}）：{line}")
    finally:
        chat.sending = False
    if chat.dirty:
        chat.dirty = False
        schedule_reply(chat)


def opener_due(chat: Chat) -> bool:
    """Has this chat been quiet long enough for the bot to start a topic?"""
    pc = chat.cfg.get("proactive")
    if not logged_in or not pc or not chat.history or paused(chat) or chat.sending:
        return False
    if chat.task and not chat.task.done():
        return False
    now = time.time()
    start, end = config.ACTIVE_HOURS
    if not start <= time.localtime(now).tm_hour < end:
        return False
    if now - chat.last_open_try < OPENER_RETRY_SECONDS:
        return False
    today = time.strftime("%Y-%m-%d")
    if sum(time.strftime("%Y-%m-%d", time.localtime(t)) == today for t in chat.opener_at) >= pc["max_per_day"]:
        return False
    last = chat.history[-1]
    # Seeded by the last message so the threshold stays put between checks.
    quiet_needed = random.Random(last["time"]).uniform(*pc["quiet_minutes"]) * 60
    if last["sender"] == "我":
        quiet_needed *= 3  # they never answered my last message: don't double-text soon
    return now - last["time"] >= quiet_needed


async def open_topic(chat: Chat) -> None:
    chat.last_open_try = time.time()
    if not chat.peer:
        chat.peer = await page.evaluate("m => window.__lcFind(m)", chat.cfg["match"])
        if not chat.peer:
            log(f"[{chat.key}] 想主动开话题，但在通讯录里没找到这个对话")
            return
    quiet = int((time.time() - chat.history[-1]["time"]) / 60)
    try:
        decision = await asyncio.to_thread(reply.compose_opener, cfg_for(chat), chat.history, quiet)
    except Exception as e:  # noqa: BLE001 - keep the bot alive on any API failure
        log(f"[{chat.key}] 模型调用失败：{e}")
        return
    if decision.kind != "reply":
        log(f"[{chat.key}] 安静了 {quiet} 分钟，想主动开话题但没找到合适的：{decision.reason or '跳过'}")
        return
    await send_lines(chat, decision.lines, opener=True)
    chat.opener_at.append(time.time())


async def proactive_loop() -> None:
    while True:
        await asyncio.sleep(60)
        for chat in chats.values():
            if opener_due(chat):
                chat.task = asyncio.create_task(open_topic(chat))


def schedule_reply(chat: Chat) -> None:
    if chat.sending or chat.instructing:
        chat.dirty = True
        return
    if chat.task and not chat.task.done():
        chat.task.cancel()  # they're still typing: restart the wait with the fuller context
    chat.task = asyncio.create_task(reply_flow(chat))


async def on_push(msg: dict) -> None:
    if msg["id"] in seen_ids:
        return
    if msg["peer"] == FILEHELPER:
        seen_ids.add(msg["id"])
        await on_filehelper(msg)
        return
    chat = match_chat(msg)
    if chat is None:
        return
    seen_ids.add(msg["id"])
    if msg["time"] <= chat.forget_before:
        return  # from before I reset this chat
    chat.peer = msg["peer"]
    if already_recorded(chat, msg["sender"], msg["text"], msg["time"]):
        return
    record(chat, msg["sender"], msg["text"], msg["time"])
    log(f"[{chat.key}] {msg['sender']}：{msg['text']}")
    remember(chat)
    if msg["time"] < started_at - REPLAY_SLACK_SECONDS:
        return  # replayed from before this run: context only, not a takeover or a cue to reply
    if msg["fromMe"]:
        if seconds := takeover_seconds(chat):
            if chat.task and not chat.task.done():
                chat.task.cancel()  # whatever it was about to say, I'm handling it myself
            chat.paused_until = time.time() + seconds
            log(f"[{chat.key}] 你亲自发言了，{seconds // 60} 分钟内不自动回复")
        # In chats where I talk alongside the bot, a pending reply goes ahead with my
        # message as extra context: me chiming in doesn't mean I answered them.
        return
    if chat.cfg.get("only_when_mentioned") and not might_be_for_me(chat, msg):
        return  # keep it as context; the model judges the ones that might be for me
    schedule_reply(chat)


async def watch() -> None:
    """Report login state changes and act on the RESUME file."""
    global logged_in
    while True:
        if RESUME_FILE.exists():
            resume_all()
        try:
            ok = await page.evaluate("() => !!window.__lcInstalled")
        except Exception:  # noqa: BLE001 - page mid-navigation
            ok = False
        if ok and not logged_in:
            log("已登录，开始监听：" + "、".join(c.cfg["name"] for c in chats.values()))
            asyncio.create_task(catch_up_after_login())
        elif not ok and logged_in:
            log("网页掉线或在重新加载，如果出现二维码请重新扫码")
        logged_in = ok
        await asyncio.sleep(2)


async def main() -> None:
    global page
    DATA_DIR.mkdir(exist_ok=True)
    for key, cfg in config.TARGETS.items():
        chats[key] = Chat(key, cfg)
        load_history(chats[key])
    load_control_state()

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            PROFILE_DIR, channel="chrome", headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        await ctx.expose_function("__lcPush", on_push)
        await ctx.add_init_script(HOOK_JS)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        for attempt in range(1, 13):
            try:
                await page.goto(WX_URL)
                break
            except Exception as e:  # noqa: BLE001 - flaky proxy/network; keep trying
                log(f"打开网页失败（第 {attempt} 次），5 秒后重试：{str(e).splitlines()[0]}")
                await asyncio.sleep(5)
        else:
            raise SystemExit("连续打不开 wx2.qq.com，检查一下网络或代理")
        log("浏览器已打开；如果看到二维码，请用手机扫码登录")
        asyncio.create_task(proactive_loop())
        await watch()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("已退出")
