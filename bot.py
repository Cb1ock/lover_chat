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
import html
import collections
import hashlib
import json
import os
import random
import re
import signal
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from playwright.async_api import BrowserContext, Page, async_playwright

import config
import flirt
import invite
import llm
import memory
import prompts
import reply

ROOT = Path(__file__).parent
PROFILE_DIR = ROOT / ".wx_profile"
DATA_DIR = ROOT / "data"
PAUSE_FILE = ROOT / "PAUSE"
RESUME_FILE = ROOT / "RESUME"
SAY_FILE = ROOT / "SAY"  # "<key>|<instruction>": like writing that instruction in 文件传输助手  # touch to lift every pause (takeover/handoff) right away
CATCH_UP_SECONDS = 30 * 60  # after login/RESUME, answer a chat whose last message is theirs and this recent
REPLAY_SLACK_SECONDS = 30  # our own sends are logged with local time, the replay with server time
ENGAGED_SECONDS = 600  # in mention-only groups, messages this soon after I spoke go to the model
ASK_WAIT_SECONDS = 10 * 60  # after asking me something in 文件传输助手, wait this long for my answer
MIN_REPLY_GAP_SECONDS = 20
GIRL_MEMORY_EVERY = 8  # her plans and news should be remembered soon, not after 15 messages  # at least this long between two auto replies in the same chat
# Every chat web WeChat sees, one file per chat, each line labelled me / ai / other.
# AI is told apart by the message ids the bot itself sent (persisted, so replays after a
# restart are still recognised), never by my HUMAN_MARKS prefix.
CORPUS_DIR = DATA_DIR / "corpus"
BOT_SENT_FILE = CORPUS_DIR / "_bot_sent_ids.txt"
CORPUS_CONTEXT = 10  # how many of their latest messages are kept as context for my next reply
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
NOTE_RE = re.compile(re.escape(NOTE_PREFIX) + r"(.+?) (?:需要你接手|想问你|适合约她|刚回了|可以撩一下)")
FLIRT_RE = re.compile(r"骚的：(.*)\n普通：(.*)")
NORMAL_RE = re.compile(r"^\s*(普通|正常|不发|算了|别发|不要)\s*[!！。.]?\s*$")
MIRROR_RE = re.compile(r"刚回了[^\n]*\n(?:.*\n)*?我：(.*)")
CORRECTION_PREFIX = "你刚才替我回了："
INVITE_RE = re.compile(r"适合约她：(.*)")
SAY_RE = re.compile(r"^(?:跟她说|和她说|回她|告诉她|你就说|就说|发)[:：，,\s]*(.*)$", re.S)
YES_RE = re.compile(r"^\s*(发|可以|好|行|ok|嗯|发吧|就这样)\s*[!！。.]?\s*$", re.I)
ASK_RE = re.compile(r"想问你：(.*)")
HOOK_JS = (ROOT / "hook.js").read_text(encoding="utf-8")
WX_URL = "https://wx2.qq.com/"
FILEHELPER = "filehelper"


@dataclass
class Chat:
    key: str
    cfg: dict
    peer: str = ""  # web WeChat UserName; changes on every login, so learned from traffic
    asked_at: float = 0.0  # when the bot last asked me something in 文件传输助手 (handoff/ask/invite)
    flirt_pending: dict | None = None  # {"flirt", "normal", "at"} waiting for me to pick in 文件传输助手
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
ctx: BrowserContext
chats: dict[str, Chat] = {}
seen_ids: set[str] = set()
started_at = time.time()
logged_in = False
filehelper_rows: list[dict] = []  # my notes and instructions in 文件传输助手
done_instructions: set[str] = set()
bot_sent_ids: set[str] = set()  # ids of every message the bot sent, across restarts
bot_texts: dict[str, list[int]] = {}  # text -> times, from history rows the bot sent (auto)
corpus_ids: dict[Path, set[str]] = {}  # per corpus file: ids already written
corpus_context: dict[Path, collections.deque] = {}  # per corpus file: their unwritten recent messages
peer_names: dict[str, tuple[str, bool]] = {}  # UserName -> (chat name, is group), learned from traffic


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


def corpus_path(name: str) -> Path:
    return CORPUS_DIR / (re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") + ".jsonl")


def corpus_write(name: str, is_group: bool, row: dict) -> None:
    """Log a row. Other people's messages wait in memory and are only written, as context,
    once I (or the bot as me) reply in that chat: chats I never speak in leave no trace."""
    path = corpus_path(name or "unknown")
    if path not in corpus_ids:
        corpus_ids[path] = ({json.loads(l)["id"] for l in path.read_text(encoding="utf-8").splitlines() if l.strip()}
                            if path.exists() else set())
    if row["id"] in corpus_ids[path]:
        return  # replayed after login, or the echo of a send already logged
    if row["author"] == "other":
        corpus_context.setdefault(path, collections.deque(maxlen=CORPUS_CONTEXT)).append(row)
        return
    rows = [r for r in corpus_context.pop(path, []) if r["id"] not in corpus_ids[path]] + [row]
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            corpus_ids[path].add(r["id"])
            f.write(json.dumps({"chat": name, "group": is_group, **r}, ensure_ascii=False) + "\n")


def collect(msg: dict) -> None:
    """Log any message from any chat. Mine are "me" unless the bot sent them."""
    name = msg["peerRemark"] or msg["peerNick"]
    if msg["peer"] == FILEHELPER:
        name = "文件传输助手"
    if name:
        peer_names[msg["peer"]] = (name, msg["isGroup"])
        adopt_unnamed(msg["peer"], name)
    else:
        # A group web WeChat hasn't loaded yet: file it under its UserName for now and
        # merge it into the named file once a later message carries the name.
        name = peer_names.get(msg["peer"], (msg["peer"], msg["isGroup"]))[0]
    if not collectable(name):
        return
    author = "other"
    if msg["fromMe"]:
        author = "ai" if sent_by_bot(msg) else "me"
    corpus_write(name, msg["isGroup"], {
        "id": msg["id"], "time": int(msg["time"]), "sender": msg["sender"], "author": author,
        "marked": author == "me" and bool(prompts.HUMAN_MARK_RE.match(msg["text"])),
        "type": msg["type"], "text": msg["text"],
    })


def sent_by_bot(msg: dict) -> bool:
    """By id when the collector saw the send; else by the bot's own records (history rows
    marked auto, my 文件传输助手 notes), for messages sent before the collector existed."""
    if msg["id"] in bot_sent_ids or msg["text"].startswith(NOTE_PREFIX):
        return True
    return any(abs(t - msg["time"]) <= 60 for t in bot_texts.get(msg["text"], ()))


def load_bot_texts() -> None:
    for path in [*DATA_DIR.glob("history_*.jsonl"), *DATA_DIR.glob("archive/*/history_*.jsonl")]:
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line) if line.strip() else {}
            if row.get("auto"):
                bot_texts.setdefault(row["text"], []).append(row["time"])


def collectable(name: str) -> bool:
    """Only her chats and ones I list in COLLECT_ONLY are logged; COLLECT_EXCLUDE always wins."""
    if any(x in name for x in getattr(config, "COLLECT_EXCLUDE", [])):
        return False
    hers = [v for c in config.TARGETS.values() if c.get("kind") == "girl" for v in c["match"].values()]
    return any(x and x in name for x in hers + list(getattr(config, "COLLECT_ONLY", [])))


def adopt_unnamed(peer: str, name: str) -> None:
    """Fold what was logged under a chat's UserName (name unknown back then) into its named file."""
    unnamed, named = corpus_path(peer), corpus_path(name)
    if unnamed == named:
        return
    if unnamed in corpus_context:
        corpus_context.setdefault(named, collections.deque(maxlen=CORPUS_CONTEXT)).extend(corpus_context.pop(unnamed))
    if not unnamed.exists():
        return
    for line in unnamed.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            row.pop("chat", None)
            is_group = row.pop("group", peer.startswith("@@"))
            corpus_write(name, is_group, row)
    unnamed.unlink()
    corpus_ids.pop(unnamed, None)


def collect_sent(peer: str, fallback_name: str, text: str, msg_id: str) -> None:
    """The bot just sent something: remember its id and log it as "ai"."""
    if not msg_id:
        return
    bot_sent_ids.add(msg_id)
    with BOT_SENT_FILE.open("a", encoding="utf-8") as f:
        f.write(msg_id + "\n")
    name, is_group = peer_names.get(peer, (fallback_name, peer.startswith("@@")))
    if not collectable(name):
        return
    corpus_write(name, is_group, {"id": msg_id, "time": int(time.time()), "sender": "我", "author": "ai",
                                  "marked": False, "type": 1, "text": text})


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
    """The chat's config plus its long-term memory and my real exchanges, as the prompts expect it."""
    mem = memory.load(chat.key)
    return {**chat.cfg, "key": chat.key, "memory": mem["facts"], "pinned": mem["pinned"], "corrections": mem["corrections"],
            "examples": prompts.real_examples(chat.key, chat.cfg["kind"], stage=chat.cfg.get("stage"))}


def remember(chat: Chat) -> None:
    """Fold piled-up messages into long-term memory in the background."""
    if chat.memory_task and not chat.memory_task.done():
        return

    async def run() -> None:
        try:
            every = GIRL_MEMORY_EVERY if chat.cfg.get("kind") == "girl" else memory.EVERY
            folded = await asyncio.to_thread(memory.catch_up, chat.key, minimum=every)
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


def catch_up(max_age: float = CATCH_UP_SECONDS) -> None:
    """Answer chats whose last message is theirs, recent, and still unanswered."""
    for chat in chats.values():
        last = chat.history[-1] if chat.history else None
        if not (chat.peer and last) or paused(chat) or last["sender"] == "我":
            continue
        if time.time() - last["time"] > max_age:
            continue
        if chat.cfg.get("only_when_mentioned") and not mentions_me(last["text"]):
            continue
        schedule_reply(chat)


async def catch_up_after_login() -> None:
    await asyncio.sleep(5)  # let the post-login replay land first
    if not logged_in:
        return  # dropped again in the meantime; the next login runs this anew
    for chat in chats.values():
        if not chat.peer:  # no replayed traffic for this chat: look it up in the contact list
            chat.peer = await find_peer(chat)
        remember(chat)
    catch_up()


async def find_peer(chat: Chat) -> str:
    try:
        return await page.evaluate("m => window.__lcFind ? window.__lcFind(m) : ''", chat.cfg["match"])
    except Exception:  # noqa: BLE001 - page reloading or logged out
        return ""


def resume_all() -> None:
    RESUME_FILE.unlink(missing_ok=True)
    RESUMED_AT_FILE.write_text(str(time.time()))
    log("收到 RESUME，所有对话恢复自动回复")
    for chat in chats.values():
        chat.paused_until = 0.0
    save_pauses()
    catch_up(max_age=12 * 3600)  # RESUME = "go on": also answer what piled up while I was away


def typing_delay(line: str) -> float:
    return min(1.0 + 0.35 * len(line) + random.uniform(0, 1.5), 8.0)


async def send(peer: str, text: str) -> dict:
    return await page.evaluate("([p, t]) => window.__lcSend(p, t)", [peer, text])


async def upload_media(path: Path, peer: str) -> str:
    """Upload a file to web WeChat's media server the way the web client does; return MediaId."""
    info = await page.evaluate("() => window.__lcUploadInfo()")
    data = path.read_bytes()
    mime = prompts.STICKER_TYPES[path.suffix.lower()]
    # Mirrors the web client's own uploader. UploadType must be 1: with 2 the server
    # accepts the request and then never answers.
    request = {
        **info["baseRequest"], "UploadType": 1, "ClientMediaId": int(time.time() * 1000),
        "TotalLen": len(data), "StartPos": 0, "DataLen": len(data), "MediaType": 4,
        "FromUserName": info["me"], "ToUserName": peer, "FileMd5": hashlib.md5(data).hexdigest(),
    }
    form = {
        "id": "WU_FILE_0", "name": path.name, "type": mime,
        "lastModifiedDate": time.strftime("%a %b %d %Y %H:%M:%S GMT+0800 (CST)"),
        "size": str(len(data)), "mediatype": "pic",
        "uploadmediarequest": json.dumps(request, separators=(",", ":")),
        "webwx_data_ticket": info["dataTicket"], "pass_ticket": urllib.parse.unquote(info["passTicket"]),
    }
    url = info["uploadUrl"]
    cookies = await ctx.cookies([url])
    headers = {
        "Cookie": "; ".join(f"{c['name']}={c['value']}" for c in cookies),
        "Origin": "https://wx2.qq.com", "Referer": "https://wx2.qq.com/",
        "User-Agent": await page.evaluate("() => navigator.userAgent"),
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url + "?f=json", data=form, files={"filename": (path.name, data, mime)},
                              headers=headers)
    try:
        media_id = r.json().get("MediaId")
    except ValueError:
        media_id = None
    if not media_id:
        raise RuntimeError(f"{url} HTTP {r.status_code}: {r.text[:200]}")
    return media_id


def sticker_jpeg(path: Path) -> str:
    """First frame of a sticker as base64 JPEG, for the vision model."""
    import base64
    import io
    from PIL import Image
    im = Image.open(path).convert("RGBA")
    flat = Image.new("RGB", im.size, "white")
    flat.paste(im, mask=im.split()[3])
    buf = io.BytesIO()
    flat.save(buf, "JPEG")
    return base64.b64encode(buf.getvalue()).decode()


async def sticker_fits(chat: Chat, path: Path) -> tuple[bool, str]:
    context = prompts.transcript(chat.history[-8:])
    try:
        return await asyncio.to_thread(llm.sticker_fits, sticker_jpeg(path), "image/jpeg", context)
    except Exception as e:  # noqa: BLE001 - can't look: don't send it
        return False, f"看不了图：{str(e)[:80]}"


async def send_sticker(peer: str, path: Path) -> dict:
    try:
        media_id = await upload_media(path, peer)
    except Exception as e:  # noqa: BLE001 - report, don't crash
        return {"ok": False, "step": "upload", "detail": repr(e)[:300]}
    return await page.evaluate("([p, m]) => window.__lcSendEmoticon(p, m)", [peer, media_id])


async def send_line(chat: Chat, line: str) -> dict:
    """Send one line of a reply: a [表情包:名字] line becomes a sticker, the rest text."""
    m = prompts.STICKER_LINE.match(line)
    if m and not chat.cfg.get("stickers", True):
        return {"ok": False, "step": "stickers off for this chat"}  # never send the raw [表情包:…] text
    if m and (path := prompts.stickers().get(m.group(1))):
        if chat.cfg.get("sticker_check"):
            fits, why = await sticker_fits(chat, path)
            if not fits:
                log(f"[{chat.key}] 表情包「{path.stem}」不合适，没发：{why}")
                return {"ok": True, "id": "", "skipped": True}
        return await send_sticker(chat.peer, path)
    if m:
        return {"ok": True, "id": "", "skipped": True}  # unknown sticker name: never send the raw text
    return await send(chat.peer, line)


async def hand_off(chat: Chat, reason: str) -> None:
    chat.paused_until = time.time() + config.HANDOFF_PAUSE_SECONDS
    chat.asked_at = time.time()
    save_pauses()
    last = chat.history[-1]["text"] if chat.history else ""
    note = (f"{NOTE_PREFIX}{chat.cfg['name']} 需要你接手：{reason}\n最后一条：{last}\n"
            f"直接在这里告诉我怎么回，我照着回")
    log(f"[{chat.key}] 转交给你：{reason}")
    log_filehelper(note, time.time())
    res = await send(FILEHELPER, note)
    collect_sent(FILEHELPER, "文件传输助手", note, res.get("id", ""))


def chat_of_note(note: str) -> Chat | None:
    """The chat a note was about: "【lover_chat】<name> 需要你接手：…" or "… 想问你：…"."""
    m = NOTE_RE.match(note)
    return next((c for c in chats.values() if m and c.cfg["name"] == m.group(1)), None)


def with_question(note: str, answer: str) -> str:
    """If the note asked me something, hand the model the question along with my answer;
    a "发" to an invitation suggestion means: send that line as it is."""
    if m := FLIRT_RE.search(note):
        if YES_RE.match(answer):
            return f"原话发这句：{m.group(1)}"
        if NORMAL_RE.match(answer):
            return f"原话发这句：{m.group(2)}" if m.group(2) != "（不回）" else "你建议撩她，我说算了：这次不回，输出 [SKIP]"
        return f"你建议撩她：{m.group(1)}\n我的意思：{answer}"
    if m := MIRROR_RE.search(note):
        return f"{CORRECTION_PREFIX}{m.group(1)}\n我的纠正：{answer}\n需要的话按纠正补一句或改口，不需要就输出 [SKIP]"
    if (m := INVITE_RE.search(note)) and YES_RE.match(answer):
        return f"原话发这句：{m.group(1)}"
    if m:
        return f"你建议约她：{m.group(1)}\n我的意思：{answer}"
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
    if len(chats) == 1:
        return next(iter(chats.values())), text
    notes = [r for r in filehelper_rows
             if r["text"].startswith(NOTE_PREFIX) and r["time"] <= ts + NOTE_ORDER_SLACK_SECONDS]
    if notes and (chat := chat_of_note(notes[-1]["text"])):
        return chat, with_question(notes[-1]["text"], text)
    return None, text


def parse_record(raw: str) -> list[tuple[str, str, float]]:
    """(sender name, text, time) of each line in a forwarded chat record's XML."""
    xml = html.unescape(html.unescape(raw.replace("<br/>", "\n")))
    m = re.search(r"<recorditem>(.*?)</recorditem>", xml, re.S)
    body = html.unescape(m.group(1)) if m else xml
    out = []
    for item in re.findall(r"<dataitem\b(.*?)</dataitem>", body, re.S):
        get = lambda tag: (re.search(rf"<{tag}>(.*?)</{tag}>", item, re.S) or [None, ""])[1].strip()
        name, text = get("sourcename"), get("datadesc") or get("datatitle")
        ts = get("srcMsgCreateTime") or get("sourcetime")
        try:
            when = float(ts) if ts.isdigit() else time.mktime(time.strptime(ts[:16], "%Y-%m-%d %H:%M"))
        except ValueError:
            when = 0.0
        if name and text:
            out.append((name, text, when))
    return out


def merge_record(msg: dict) -> bool:
    """I forwarded a stretch of chat I had myself: add it to that chat's history as context."""
    rows = parse_record(msg.get("raw", ""))
    if not rows:
        return False
    names = {n for n, _, _ in rows}
    chat = next((c for c in chats.values() if c.cfg["name"] in names or c.cfg["match"].get("remark") in names), None)
    if chat is None and len(chats) == 1:
        chat = next(iter(chats.values()))
    if chat is None:
        log(f"[文件传输助手] 收到转发的聊天记录（{'、'.join(sorted(names))}），但对不上监控的对话")
        return True
    me = msg.get("myNick") or ""
    last = chat.history[-1]["time"] if chat.history else 0
    added = 0
    for i, (name, text, when) in enumerate(rows):
        sender = "我" if name == me or (me == "" and name != chat.cfg["name"]) else chat.cfg["name"]
        when = max(when or msg["time"] - len(rows) + i, last)
        if already_recorded(chat, sender, text, when):
            continue
        record(chat, sender, text, when)
        last, added = when, added + 1
    log(f"[{chat.key}] 从你转发的聊天记录里补了 {added} 条（共 {len(rows)} 条）")
    remember(chat)
    if chat.history and chat.history[-1]["sender"] == "我":
        log(f"[{chat.key}] 最后一条是你说的，等她回")
    else:
        schedule_reply(chat)
    return True


async def on_filehelper(msg: dict) -> None:
    text = msg["text"]
    log_filehelper(text, msg["time"])
    if not msg["fromMe"] or text.startswith(NOTE_PREFIX):
        return
    if msg.get("raw") and ("recorditem" in msg["raw"] or "暂不支持" in text):
        if merge_record(msg):
            return
        if "暂不支持" in text:
            log(f"[文件传输助手] 收到网页版看不了的消息，不当成指示：{msg['raw'][:200]}")
            return
    if "暂不支持的消息类型" in text:
        (DATA_DIR / "_unsupported_debug.json").write_text(msg.get("raw", ""), encoding="utf-8")
        log("[文件传输助手] 收到网页版看不了的消息，不当成指示")
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
    quoted_note = bool(QUOTE_RE.match(text))
    open_question = time.time() - chat.asked_at < ASK_WAIT_SECONDS
    say = SAY_RE.match(instruction)
    if say:
        instruction = say.group(1).strip() or instruction
    elif not (quoted_note or open_question or reply.CARRY_ON.match(instruction.strip())
              or instruction.startswith(("原话发这句", "你之前问我", "你建议约她", CORRECTION_PREFIX))):
        # Nothing was asked and it doesn't say "tell her": it's about how the bot talks.
        memory.add_correction(chat.key, instruction.strip())
        log(f"[{chat.key}] 记住了你的意见：{instruction.strip()}")
        return
    chat.asked_at = 0.0
    chat.flirt_pending = None
    if m := re.match(r"^(?:记住[:：，,\s]*)?((?:别|不要|以后|少|多)\S.*)", instruction, re.S) \
            or re.match(r"^记住[:：，,\s]*(.+)", instruction, re.S):
        memory.add_correction(chat.key, m.group(1).strip())  # a rule for later, nothing to send now
        log(f"[{chat.key}] 记住了你的纠正：{m.group(1).strip()}")
        return
    chat.paused_until = 0.0
    save_pauses()
    log(f"[{chat.key}] 收到你的指示：{instruction}")
    if instruction.startswith(CORRECTION_PREFIX):
        fix = instruction.split("我的纠正：", 1)[1].split("\n", 1)[0]
        said = instruction[len(CORRECTION_PREFIX):].split("\n", 1)[0]
        memory.add_correction(chat.key, f"（我回了“{said}”）{fix}")
    if chat.task and not chat.task.done():
        chat.task.cancel()
    chat.task = asyncio.create_task(instructed_flow(chat, instruction))


async def instructed_flow(chat: Chat, instruction: str) -> None:
    chat.instructing = True
    try:
        await asyncio.sleep(random.uniform(2, 5))
        if not chat.peer:
            chat.peer = await find_peer(chat)
        try:
            decision = await asyncio.to_thread(reply.compose_instructed, cfg_for(chat), chat.history, instruction)
        except Exception as e:  # noqa: BLE001 - keep the bot alive on any API failure
            log(f"[{chat.key}] 模型调用失败：{e}")
            return
        if decision.kind == "ask":
            await ask_me(chat, decision.reason)
        elif decision.kind == "handoff":
            await hand_off(chat, decision.reason)
        elif decision.kind == "skip":
            log(f"[{chat.key}] 按指示没发：{decision.reason}")
        await send_lines(chat, decision.lines if decision.kind == "reply" else [])
    finally:
        chat.instructing = False
        if chat.dirty:  # they wrote while I was being obeyed: answer that next
            chat.dirty = False
            schedule_reply(chat)


async def reply_flow(chat: Chat) -> None:
    await asyncio.sleep(random.uniform(*config.READ_DELAY))
    if chat.sent_at:  # don't fire replies back-to-back: leave a human-ish gap since the last one
        await asyncio.sleep(max(0.0, MIN_REPLY_GAP_SECONDS - (time.time() - chat.sent_at[-1])))
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
    suggestion = None
    if invite.due(chat.cfg, chat.key):
        try:
            suggestion = await asyncio.to_thread(invite.judge, cfg_for(chat), chat.key, chat.history)
        except Exception as e:  # noqa: BLE001 - the judge is optional
            log(f"[{chat.key}] 邀约判断失败：{e}")
    limit = chat.cfg.get("max_chars")
    if suggestion and suggestion["kind"] == "soft" and limit and \
            prompts.reply_chars(decision.lines + [suggestion["line"]]) > limit:
        log(f"[{chat.key}] 邀约这句太长，这次不带：{suggestion['line']}")
        suggestion = None
    if suggestion and suggestion["kind"] == "soft":
        decision.lines.append(suggestion["line"])  # a light, no-commitment invite rides along
        invite.mark_invited(chat.key, suggestion["line"])
        log(f"[{chat.key}] 顺带轻邀约：{suggestion['line']}（{suggestion['reason']}）")
    if chat.cfg.get("flirt_review"):
        try:
            cheeky = await asyncio.to_thread(flirt.judge, cfg_for(chat), chat.history, decision.lines)
        except Exception as e:  # noqa: BLE001 - optional
            log(f"[{chat.key}] 撩不撩判断失败：{e}")
            cheeky = None
        if cheeky:
            await offer_flirt(chat, cheeky, decision.lines)
            return
    await send_lines(chat, decision.lines)
    if suggestion and suggestion["kind"] == "concrete":
        await suggest_invite(chat, suggestion)


FLIRT_WAIT_SECONDS = 8 * 60  # no pick by then: the normal reply goes out


async def offer_flirt(chat: Chat, cheeky: dict, normal: list[str]) -> None:
    """A cheeky line is mine to approve: put it next to the normal reply and let me pick."""
    chat.paused_until = time.time() + FLIRT_WAIT_SECONDS
    save_pauses()
    chat.asked_at = time.time()
    chat.flirt_pending = {"flirt": cheeky["line"], "normal": normal, "at": time.time()}
    last = " / ".join(r["text"] for r in chat.history[-3:] if r["sender"] != "我") or chat.history[-1]["text"]
    note = (f"{NOTE_PREFIX}{chat.cfg['name']} 可以撩一下\n她：{last}\n"
            f"骚的：{cheeky['line']}\n普通：{' / '.join(normal) or '（不回）'}\n"
            f"（{cheeky['why']}）回“发”发骚的，回“普通”发普通的，或者直接写你想说的；"
            f"{FLIRT_WAIT_SECONDS // 60} 分钟没回就发普通的")
    log(f"[{chat.key}] 想撩：{cheeky['line']}（等你挑）")
    log_filehelper(note, time.time())
    res = await send(FILEHELPER, note)
    collect_sent(FILEHELPER, "文件传输助手", note, res.get("id", ""))
    asyncio.create_task(flirt_timeout(chat, chat.flirt_pending))


async def flirt_timeout(chat: Chat, pending: dict) -> None:
    await asyncio.sleep(FLIRT_WAIT_SECONDS)
    if chat.flirt_pending is not pending:
        return  # I picked, or a newer offer replaced this one
    chat.flirt_pending = None
    chat.paused_until = 0.0
    save_pauses()
    if chat.history and chat.history[-1]["sender"] == "我":
        return  # I answered her myself meanwhile
    log(f"[{chat.key}] 你没挑，发普通的")
    if pending["normal"]:
        await send_lines(chat, pending["normal"])
    if chat.history and chat.history[-1]["sender"] != "我":
        schedule_reply(chat)  # she wrote more while we waited


async def suggest_invite(chat: Chat, suggestion: dict) -> None:
    """A concrete plan (a day, a place) is mine to commit to: suggest it, don't send it."""
    chat.asked_at = time.time()
    invite.mark_invited(chat.key, suggestion["line"])
    note = (f"{NOTE_PREFIX}{chat.cfg['name']} 适合约她：{suggestion['line']}\n理由：{suggestion['reason']}\n"
            f"回复“发”就原样发出去，或者写你想怎么说（最好引用这条）")
    log(f"[{chat.key}] 建议邀约：{suggestion['line']}")
    log_filehelper(note, time.time())
    res = await send(FILEHELPER, note)
    collect_sent(FILEHELPER, "文件传输助手", note, res.get("id", ""))


async def ask_me(chat: Chat, question: str) -> None:
    """The bot needs a fact only I know: ask in 文件传输助手 and hold the chat until I answer."""
    chat.paused_until = time.time() + ASK_WAIT_SECONDS
    chat.asked_at = time.time()
    save_pauses()
    last = chat.history[-1]["text"] if chat.history else ""
    note = (f"{NOTE_PREFIX}{chat.cfg['name']} 想问你：{question}\n对方最后一条：{last}\n"
            f"直接在这里回（最好引用这条），我照你的意思回")
    log(f"[{chat.key}] 问你：{question}")
    log_filehelper(note, time.time())
    res = await send(FILEHELPER, note)
    collect_sent(FILEHELPER, "文件传输助手", note, res.get("id", ""))


WECHAT_FACE = re.compile(r"\[[\u4e00-\u9fa5A-Za-z]{1,6}\]")


async def send_lines(chat: Chat, lines: list[str], *, opener: bool = False) -> None:
    if not chat.cfg.get("faces", True):
        # WeChat's own faces ([旺柴], [捂脸]) off here: I'd rather use emoji.
        lines = [s for s in (WECHAT_FACE.sub("", l).strip() for l in lines) if s]
    chat.sending = True
    sent: list[str] = []
    try:
        for line in lines:
            await asyncio.sleep(typing_delay(line))
            if paused(chat):
                break
            res = await send_line(chat, line)
            if not res["ok"]:
                log(f"[{chat.key}] 发送失败：{line} {res.get('step', '')} {res.get('detail') or ''}")
                break
            if res.get("skipped"):
                continue  # a sticker that didn't fit: nothing went out
            seen_ids.add(res["id"])
            collect_sent(chat.peer, chat.cfg["name"], line, res["id"])
            record(chat, "我", line, time.time(), auto=True, opener=opener)
            chat.sent_at.append(time.time())
            log(f"[{chat.key}] 我（{'主动' if opener else '自动'}）：{line}")
            sent.append(line)
    finally:
        chat.sending = False
    if sent and chat.cfg.get("mirror"):
        await mirror(chat, sent, opener)
    if chat.dirty:
        chat.dirty = False
        schedule_reply(chat)


async def mirror(chat: Chat, sent: list[str], opener: bool) -> None:
    """Copy what I just said to 文件传输助手, with what it answered, so I can check it."""
    before = [r for r in chat.history if r["text"] not in sent or r["sender"] != "我"]
    theirs = []
    for r in reversed(before):
        if r["sender"] == "我":
            break
        theirs.insert(0, r["text"])
    head = "（主动开的话题）" if opener or not theirs else "她：" + " / ".join(theirs[-4:])
    note = (f"{NOTE_PREFIX}{chat.cfg['name']} 刚回了\n{head}\n我：{' / '.join(sent)}\n"
            f"有不对的引用这条告诉我，我会改口并记住")
    log_filehelper(note, time.time())
    try:
        res = await send(FILEHELPER, note)
        collect_sent(FILEHELPER, "文件传输助手", note, res.get("id", ""))
    except Exception as e:  # noqa: BLE001 - the copy is a convenience, never block the chat
        log(f"[{chat.key}] 抄送文件传输助手失败：{e}")


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
        chat.peer = await find_peer(chat)
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


async def look_at(chat: Chat, msg: dict) -> str:
    """"[图片：…]" with what the picture shows, or plain "[图片]" if I can't see it."""
    try:
        img = await page.evaluate("id => window.__lcImage ? window.__lcImage(id) : null", msg["id"])
        if not img or not img.get("ok"):
            raise RuntimeError((img or {}).get("detail", "下载失败"))
        desc = await asyncio.to_thread(llm.describe_image, img["data"], img["mime"])
        if desc:
            log(f"[{chat.key}] 看了图：{desc}")
            return f"[图片：{desc}]"
    except Exception as e:  # noqa: BLE001 - without sight the bot asks me instead
        log(f"[{chat.key}] 看图失败：{str(e)[:120]}")
    return msg["text"]


async def on_push(msg: dict) -> None:
    collect(msg)
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
    if msg["type"] == 3 and not msg["fromMe"] and msg["time"] >= started_at - REPLAY_SLACK_SECONDS:
        msg["text"] = await look_at(chat, msg)
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


async def sticker_self_test(name: str) -> None:
    """LC_STICKER_TEST=<name>: after login, send stickers/<name>.* to 文件传输助手."""
    await asyncio.sleep(3)
    path = next(iter(sorted(prompts.STICKER_DIR.glob(f"{name}.*"))), None)
    if path is None:
        log(f"[表情包测试] 没找到 stickers/{name}.*")
        return
    res = await send_sticker(FILEHELPER, path)
    collect_sent(FILEHELPER, "文件传输助手", f"[表情包:{path.stem}]", res.get("id", ""))
    log(f"[表情包测试] {path.name} 发给文件传输助手：{'成功' if res['ok'] else '失败'} {res}")
    if not res["ok"]:
        # Local-only debugging aid: what the upload needs, so it can be retried by hand.
        info = await page.evaluate("() => window.__lcUploadInfo()")
        dump = {"info": info, "cookies": await ctx.cookies([info["uploadUrl"]]),
                "ua": await page.evaluate("() => navigator.userAgent")}
        (DATA_DIR / "_upload_debug.json").write_text(json.dumps(dump, ensure_ascii=False))
        log("[表情包测试] 调试信息写到 data/_upload_debug.json（调通后删掉）")


async def watch() -> None:
    """Report login state changes and act on the RESUME file."""
    global logged_in
    while True:
        if RESUME_FILE.exists():
            resume_all()
        if SAY_FILE.exists() and logged_in:
            key, _, instruction = SAY_FILE.read_text(encoding="utf-8").strip().partition("|")
            SAY_FILE.unlink()
            if (chat := chats.get(key)) and instruction:
                log(f"[{key}] 收到指示（SAY）：{instruction}")
                chat.paused_until = 0.0
                save_pauses()
                if chat.task and not chat.task.done():
                    chat.task.cancel()  # one reply at a time: my instruction replaces the bot's own
                chat.task = asyncio.create_task(instructed_flow(chat, instruction))
        try:
            ok = await page.evaluate("() => !!window.__lcInstalled")
        except Exception:  # noqa: BLE001 - page mid-navigation
            ok = False
        if ok and not logged_in:
            log("已登录，开始监听：" + "、".join(c.cfg["name"] for c in chats.values()))
            asyncio.create_task(catch_up_after_login())
            if test := os.environ.get("LC_STICKER_TEST"):
                asyncio.create_task(sticker_self_test(test))
        elif not ok and logged_in:
            log("网页掉线或在重新加载，如果出现二维码请重新扫码")
        logged_in = ok
        await asyncio.sleep(2)


async def main() -> None:
    global page, ctx
    DATA_DIR.mkdir(exist_ok=True)
    for key, cfg in config.TARGETS.items():
        if not cfg.get("enabled", True):
            continue
        chats[key] = Chat(key, cfg)
        load_history(chats[key])
    load_control_state()
    CORPUS_DIR.mkdir(exist_ok=True)
    load_bot_texts()
    if BOT_SENT_FILE.exists():
        bot_sent_ids.update(BOT_SENT_FILE.read_text(encoding="utf-8").split())

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
                await page.goto(WX_URL, wait_until="domcontentloaded", timeout=60000)
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
    # launchd stops/restarts with SIGTERM: shut down as cleanly as on Ctrl+C.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("已退出")
