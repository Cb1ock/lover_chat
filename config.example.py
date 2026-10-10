"""Who the bot replies to and how. Copy to config.py, fill in, restart bot.py to apply.

config.py is gitignored: it holds your persona, your contacts' names and real quotes.
"""
from pathlib import Path

# One line saying what I'm doing right now, e.g. “在公司上班，下午一直开会”.
# Re-read before every reply, so edit it any time without restarting.
STATUS_FILE = Path(__file__).with_name("status.txt")

MY_NAMES = ["你的微信昵称"]  # how I show up in chats; used to spot @s and quote-replies to me
CALL_NAMES = ["你的外号"]    # what people call me in plain text; counts as possibly talking to me
HUMAN_MARK = "me"           # I start a message with “me:” to show it's really me typing; the bot never uses it

# About me. The model is told never to invent facts, so anything not written here
# gets answered vaguely. The more concrete this is, the more it sounds like me.
ME = """\
- 微信名：你的微信昵称；男生/女生，朋友叫我“你的外号”
- 做什么工作、在哪个城市、平时作息
- 爱好、最近在忙的事、性格
- 说话习惯：常用的口头禅、表情
"""

# Things I've actually said, so the model picks up my wording. Add 20~50 real ones.
# These are used in every chat; put chat-specific ones under that target's "samples".
MY_SAMPLES = [
    "哈哈哈", "真的假的", "🐶",
]

# kind: "girl" | "friend" | "group" | "big_group" picks the matching rules in prompts.py.
# match: "remark" = the 备注名 of a friend; "group" = text contained in the group chat's name.
TARGETS = {
    "her": {
        "match": {"remark": "她的备注名"},
        "name": "她",
        "kind": "girl",
        "stage": "暧昧",  # 刚认识 / 暧昧 / 恋爱 — sets how close the bot may get
        "about": "我们是什么关系、认识多久、我平时怎么称呼她、她的性格、最近在忙什么、她不喜欢什么。",
        "samples": ["我平时跟她说过的原话"],
        "proactive": {"quiet_minutes": [60, 180], "max_per_day": 2},
    },
    "buddy": {
        "match": {"remark": "朋友的备注名"},
        "name": "朋友",
        "kind": "friend",
        "about": "铁哥们，说话随意，互相吐槽。",
        "proactive": {"quiet_minutes": [40, 120], "max_per_day": 3},
    },
    "squad": {
        "match": {"group": "小群群名"},
        "name": "小群",
        "kind": "group",
        "about": "几个熟人的小群，说话随意。",
        # I told this chat about the agent myself: field "is this AI?" teasing instead of handing off.
        "agent_known": True,
        "takeover_seconds": 0,  # I chat here alongside the bot: my messages don't pause it
    },
    "big": {
        "match": {"group": "大群群名"},
        "name": "大群",
        "kind": "big_group",
        "about": "人比较多的群，不一定都熟。说话克制一点。",
        # Only messages that might be for me (@, quote, my nickname, or right after I spoke)
        # reach the model, which then decides whether they really are.
        "only_when_mentioned": True,
    },
}

# "proactive" on a target lets the bot open a topic once the chat has been quiet for a
# random number of minutes in quiet_minutes (3x that if my message was the last one),
# at most max_per_day times, and only between these hours.
ACTIVE_HOURS = (10, 23)

READ_DELAY = (4, 10)            # seconds after their last message before replying; collects bursts
HISTORY_TURNS = 40              # messages of context sent to the model
HUMAN_TAKEOVER_SECONDS = 10 * 60  # I typed in a chat myself -> bot stays quiet there this long
                                  # (a target's "takeover_seconds" overrides it; 0 = never pause)
HANDOFF_PAUSE_SECONDS = 30 * 60   # bot asked me to take over -> stays quiet this long
MAX_LINES_PER_HOUR = 40         # per chat; beyond this the bot hands off instead of replying
