# AGENTS.md

Notes for coding agents working on this repo. See README.md for what the project does.

## Layout

- `bot.py`: asyncio main loop. Owns the Playwright browser, per-chat state (`Chat`), message
  dispatch (`on_push`), pauses/handoffs, the 文件传输助手 control channel, proactive openers,
  and scheduling of memory updates.
- `hook.js`: injected into wx2.qq.com via `add_init_script`. Wraps the Angular
  `chatFactory.messageProcess` to forward every message to Python (`window.__lcPush`). It also
  exposes `window.__lcSend(peer, text)` for sending and `window.__lcFind(match)` for looking up a
  chat's UserName.
- `reply.py`: pure functions from (target config, history) to a `Decision`
  (`reply` / `skip` / `handoff`). It runs the deterministic checks, then the LLM reviewer, then
  rewrites.
- `prompts.py`: every prompt template plus the regex rules (`SUSPICION`, `GIRL_HANDOFF`,
  `COMMITMENT`, `AI_TOPIC`, `AI_TELL`, repetition and parrot checks).
- `memory.py`: long-term memory per chat in `data/memory_<key>.json`: `pinned` (hand-written, never
  rewritten) and `facts` (extracted every few messages).
- `invite.py`: the invitation judge, a separate model call per reply in girl chats past their stage
  cooldown. A soft invite rides along with the reply; a concrete one goes to 文件传输助手 for the user to
  approve ("发") or rewrite. State in `data/invites_<key>.json`.
- `flirt.py`: the flirt judge. When she leaves an opening it drafts one cheeky line; `bot.offer_flirt`
  puts it in 文件传输助手 next to the normal reply and the user picks ("发" / "普通"), else the normal
  one goes out after `FLIRT_WAIT_SECONDS`. It never sends on its own.
- `tools/rehearse.py`: offline rehearsal of one chat (same pipeline, nothing sent or saved).
- `llm.py`: `chat()` to DeepSeek through DashScope's OpenAI-compatible API (`reasoning_effort=None`
  means no thinking, fast), plus `describe_image` / `sticker_fits` on a Gemini-style endpoint
  (`VISION_URL` / `VISION_MODELS` / `VISION_KEYS` in `.env`). DeepSeek accepts images but makes up
  what's in them: never use it for vision.
- `config.py`: personal and gitignored. `config.example.py` documents every key.

## Invariants (don't break these)

- **Never invent facts about the user.** In chats with `guardrails` on (the default, meant for
  her), also never promise anything on their behalf, and hand off when she asks to meet, gets upset,
  or suspects a bot. `guardrails: False` (the user's guy friends) drops the promise checks and the
  handoffs. When she asks to meet or asks about the user's own plans, the bot asks the user in
  文件传输助手 (「她问你……，怎么回？」, `reply.ask_line`) instead of answering. Prompt rules alone don't hold: back them with code checks in `prompts.py` and the
  reviewer in `reply.py`.
- **The bot must never use the user's `HUMAN_MARKS` prefixes.** It is stripped from the transcript
  shown to the model (`transcript`) and from model output (`parse_reply`).
- **Web WeChat replays recent messages after every login.** Dedupe against the whole history
  file (`Chat.seen_rows`, `REPLAY_SLACK_SECONDS`). Messages older than `started_at` are context
  only: they never trigger a pause or a reply.
- **The bot's own sends echo back through the hook.** `hook.js` filters them (`botSent`), and
  Python records them itself with `auto: true`. Pause logic relies on that flag to tell the user's
  own messages from the bot's.
- **Web WeChat UserNames change on every login.** Match chats by 备注名 or group name, never by
  UserName.
- **A failing style check ends in `skip`, not `handoff`.** Only real "the user should handle this"
  cases hand off: suspicion, invites, anger, money, and the reviewer's handoff verdict.
- **Instructions in 文件传输助手 override every check** (`compose_instructed`), except that they never
  just repeat her words. Only text that answers an open question (`Chat.asked_at`), quotes a note,
  starts with 跟她说/回她/就说 (`SAY_RE`) or says carry on (`reply.CARRY_ON`) becomes a message;
  anything else is feedback and goes into the chat's `corrections` in memory. "原话发这句：…" is sent
  verbatim.
- **The bot never guesses at pictures.** Photos get a vision description (`[图片：…]`) before they're
  recorded; if that fails, `prompts.unseen_media` makes the reply an ask. Stickers are described once
  (`stickers/_desc.json`) and, with `sticker_check`, looked at again before sending.
- **The corpus (`data/corpus/`) labels authors by message id, not by the user's marker.** The bot
  records every id it sends in `_bot_sent_ids.txt` (`collect_sent`). Rows sent before that existed
  fall back to `history_*.jsonl` rows marked `auto` (`sent_by_bot`). Only her chats (`kind: "girl"`) and
  `COLLECT_ONLY` chats are collected (`collectable`); other people's messages are only written as
  context once the user speaks; `COLLECT_EXCLUDE` always wins. The user does not want the bot to
  learn how he talks to guy friends or groups: `real_examples` only reads her own chat, and a
  hand-curated `data/style_<key>.md` wins over auto-extraction.
- **Replies are generated as a continued chat** (`prompts.chat_messages`): my messages are the
  assistant's turns, theirs are user turns, and notes such as rewrite feedback or openers go in a
  trailing system message. Style comes from real exchanges (`real_examples`), which skip
  HUMAN_MARKS lines and any talk about the bot itself (`META_RE`), or the bot learns to out itself.

## Working on prompts

- Change `prompts.py`, then run `.venv/bin/python tools/eval_girl.py <label>` and compare it with
  an earlier `data/eval_*.txt`. Nothing is sent.
- Examples in prompts get copied verbatim. Keep them generic, never take them from a real chat,
  and say "只学方法，别照抄".
- Sticky failure modes we've seen:
  - copying the transcript's `名字：` prefix;
  - echoing the other side;
  - reusing its own joke;
  - adding dates nobody mentioned;
  - describing images it can't see;
  - copying a prompt example word for word (an example "出道题…" turned into quizzing her: write
    examples as a method in parentheses when the wording is catchy);
  - nod-only replies that leave her nothing to answer (`prompts.dead_end`).

## Running

- `.venv/bin/python bot.py` starts the bot. Only one instance at a time: two instances fight over
  the Chrome profile and the web session. After SIGTERM, check with `pgrep -f bot.py` that the old
  one is gone before starting the next; it can take several seconds to close the browser.
- Restart it after code or config changes. History, memory, pauses and handled instructions are
  all persisted under `data/`, so restarts are safe.
- `data/bot.log` has every message, reply, skip and handoff.

## Never commit

`.env`, `config.py`, `status.txt`, `data/`, `.wx_profile/`, `stickers/`. They hold API keys, the user's
persona, contacts' names and private chat logs.
