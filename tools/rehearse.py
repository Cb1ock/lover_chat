"""Rehearse a chat with one target, offline: you type as them, the bot answers as you.

Nothing touches WeChat and nothing is written to data/. Uses the same pipeline as bot.py
(memory, style file, checks, reviewer), starting from the chat's saved recent history.

  .venv/bin/python tools/rehearse.py <key>             # interactive
  .venv/bin/python tools/rehearse.py <key> --stage 暧昧  # try another stage
  .venv/bin/python tools/rehearse.py <key> --fresh      # no prior history
  .venv/bin/python tools/rehearse.py <key> --script f   # her lines from a file; blank line = she hits "send"

Interactive: type her messages one per line, an empty line lets "you" reply.
/mem shows what this rehearsal would add to long-term memory (not saved), /quit stops.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import invite  # noqa: E402
import memory  # noqa: E402
import prompts  # noqa: E402
import reply  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("key")
    ap.add_argument("--stage")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--script")
    args = ap.parse_args()

    cfg = {**config.TARGETS[args.key], "key": args.key}
    if args.stage:
        cfg["stage"] = args.stage
    mem = memory.load(args.key)
    cfg["memory"], cfg["pinned"] = mem["facts"], mem["pinned"]
    cfg["examples"] = prompts.real_examples(args.key, cfg["kind"], stage=cfg.get("stage"))
    path = prompts.DATA_DIR / f"history_{args.key}.jsonl"
    history = [] if args.fresh or not path.exists() else [
        json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    them = cfg["name"]
    start_len = len(history)
    invited = False  # the bot would record an invitation and wait out the cooldown; mimic that here
    print(f"== 预演：{them}（{cfg.get('stage', cfg['kind'])}）  上下文 {len(history[-config.HISTORY_TURNS:])} 条")

    if args.script:
        turns, cur = [], []
        for line in Path(args.script).read_text(encoding="utf-8").splitlines() + [""]:
            if line.strip():
                cur.append(line.strip())
            elif cur:
                turns.append(cur)
                cur = []
    else:
        turns = None

    while True:
        if turns is not None:
            if not turns:
                show_memory(mem, history[start_len:])
                break
            batch = turns.pop(0)
            for t in batch:
                print(f"{them}：{t}")
        else:
            batch = []
            while True:
                t = input(f"{them}：").strip()
                if t == "/quit":
                    return
                if t == "/mem":
                    show_memory(mem, history[start_len:])
                    continue
                if not t:
                    break
                batch.append(t)
            if not batch:
                continue
        now = time.time()
        history += [{"sender": them, "text": t, "time": now + i * 0.01} for i, t in enumerate(batch)]
        start = time.time()
        d = reply.compose(cfg, history[-config.HISTORY_TURNS:])
        took = time.time() - start
        suggestion = None
        if d.kind == "reply" and not invited and invite.due(cfg, args.key):
            suggestion = invite.judge(cfg, args.key, history[-config.HISTORY_TURNS:])
            invited = bool(suggestion)
            if suggestion and suggestion["kind"] == "soft":
                d.lines.append(suggestion["line"])
        if d.kind == "reply":
            for line in d.lines:
                print(f"    我（AI）：{line}")
                history.append({"sender": "我", "text": line, "time": time.time(), "auto": True})
            if suggestion:
                tag = "最后一句是顺带的轻邀约" if suggestion["kind"] == "soft" else f"建议你约她：{suggestion['line']}"
                print(f"    【邀约判断】{tag}（{suggestion['reason']}）")
            print(f"    （{took:.0f} 秒）")
        else:
            label = {"skip": "不回", "handoff": "转交给你", "ask": "来问你"}[d.kind]
            print(f"    【{label}】{d.reason}")
        print()


def show_memory(mem: dict, rows: list[dict]) -> None:
    """What the summarizer would fold into long-term memory from this rehearsal (not saved)."""
    if not rows:
        return
    print("== 这段对话会新记住：")
    try:
        new = [f for f in memory.extract(mem, rows) if f not in mem["facts"]]
    except Exception as e:  # noqa: BLE001
        print(f"   （整理失败：{e}）")
        return
    for f in new or ["（没有新东西）"]:
        print(f"   + {f}")
    print()


if __name__ == "__main__":
    main()
