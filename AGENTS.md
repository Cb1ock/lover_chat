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
- `memory.py`: long-term memory per chat in `data/memory_<key>.json`.
- `llm.py`: a single `chat()` call to DeepSeek through DashScope's OpenAI-compatible API.
  `reasoning_effort=None` means no thinking (fast).
- `config.py`: personal and gitignored. `config.example.py` documents every key.

## Invariants (don't break these)

- **Never invent facts about the user or promise anything on their behalf.** Prompt rules alone
  don't hold. Back them with code checks in `prompts.py` and the reviewer in `reply.py`.
- **The bot must never use the user's `HUMAN_MARK` prefix.** It is stripped from the transcript
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
- **Instructions in 文件传输助手 override every check** (`compose_instructed`).

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
  - describing images it can't see.

## Running

- `.venv/bin/python bot.py` starts the bot. Only one instance at a time: two instances fight over
  the Chrome profile and the web session.
- Restart it after code or config changes. History, memory, pauses and handled instructions are
  all persisted under `data/`, so restarts are safe.
- `data/bot.log` has every message, reply, skip and handoff.

## Never commit

`.env`, `config.py`, `status.txt`, `data/`, `.wx_profile/`. They hold API keys, the user's
persona, contacts' names and private chat logs.
