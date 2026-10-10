"""Run the reply pipeline over fixed girl-chat scenarios, to compare prompt versions.

Usage: .venv/bin/python tools/eval_girl.py [label]
Writes data/eval_<label>.txt and prints it. Nothing is sent to WeChat.
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reply  # noqa: E402

STAGES = {
    "暧昧": "暧昧中的女生，认识两个月，还没确定关系，平时叫她名字；她在上海做产品经理，喜欢追剧和猫，最近在准备跳槽面试",
    "恋爱": "女朋友，在一起一年，我叫她“宝”；她在上海做产品经理，喜欢追剧和猫，最近在准备跳槽面试",
}

# (name, messages oldest-first as (sender, text), expected kind or None if any reply is fine)
SCENARIOS = [
    ("日常分享", [("她", "今天公司楼下新开了家奶茶"), ("她", "排了半小时队")], None),
    ("吐槽同事", [("她", "我们组那个新来的"), ("她", "开会抢我功劳"), ("她", "气死")], None),
    ("累了", [("她", "今天好累啊")], None),
    ("生病", [("她", "感冒了 嗓子好疼")], None),
    ("开心的事", [("她", "我面试过了！！")], None),
    ("发自拍", [("她", "[图片]"), ("她", "新做的头发 怎么样")], None),
    ("发美食", [("她", "[图片]"), ("她", "自己做的")], None),
    ("她开玩笑", [("她", "我决定今天开始减肥"), ("她", "从明天开始")], None),
    ("回得很短", [("我", "周末干嘛了"), ("她", "睡觉")], None),
    ("问在干嘛", [("她", "在干嘛")], None),
    ("撒娇", [("她", "好无聊 陪我聊天嘛")], None),
    ("试探", [("我", "刚在忙"), ("她", "你是不是不想理我")], None),
    ("你觉得我胖吗", [("她", "我是不是胖了")], None),
    ("追剧", [("她", "最近在看那个新出的悬疑剧"), ("她", "太上头了")], None),
    ("焦虑面试", [("她", "明天二面"), ("她", "好紧张 睡不着")], None),
    ("说晚安", [("她", "困了 睡啦")], None),
    ("早上", [("她", "早")], None),
    ("约见面", [("她", "周六有空吗"), ("她", "想去看那个展")], "handoff"),
    ("生气", [("我", "刚看到"), ("她", "你每次都这样"), ("她", "算了")], "handoff"),
    ("怀疑AI", [("她", "你今天说话怎么怪怪的"), ("她", "是你本人吗")], "handoff"),
]


def run(stage: str, name: str, msgs: list, expect: str | None) -> str:
    now = time.time()
    history = [{"sender": s, "text": t, "time": now - 30 * (len(msgs) - i)} for i, (s, t) in enumerate(msgs)]
    target = {"name": "她", "kind": "girl", "stage": stage, "about": STAGES[stage]}
    t = time.time()
    try:
        d = reply.compose(target, history)
        out = " / ".join(d.lines) if d.kind == "reply" else f"[{d.kind}] {d.reason}"
        flag = "" if expect is None or d.kind == expect else f"  ✗ 期望 {expect}"
    except Exception as e:  # noqa: BLE001
        out, flag = f"ERROR {e}", "  ✗"
    said = " | ".join(f"{s}：{x}" for s, x in msgs)
    return f"[{stage}] {name} ({time.time() - t:.0f}s)\n    {said}\n    → {out}{flag}"


def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else time.strftime("%H%M")
    jobs = [(stage, *sc) for stage in STAGES for sc in SCENARIOS]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda j: run(*j), jobs))
    text = "\n".join(results)
    out = Path(__file__).resolve().parent.parent / "data" / f"eval_{label}.txt"
    out.write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"\nsaved to {out}")


if __name__ == "__main__":
    main()
