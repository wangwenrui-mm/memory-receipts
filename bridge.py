
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ <-> DeepSeek 桥接服务
========================
链路： QQ客户端 -> QQNT -> NapCatQQ -> OneBot11 正向WebSocket -> 本程序 -> DeepSeek API

特性:
  * 只回应白名单(owner)的私聊，其他人一律沉默
  * 每个会话独立多轮记忆，落盘到 data/history.json，重启不丢
  * 图片理解（deepseek-flash 支持视觉，自动下载 QQ 图片转 base64）
  * token 用量与花费统计（考虑高峰/空闲时段差价），发 /cost 查看
  * DeepSeek API 失败自动重试 + 兜底提示
  * WebSocket 断线自动重连
  * 长回复自动分段发送
  * 命令: /reset /help /ping /cost

依赖: pip install websockets
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import random
import re
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:
    print("缺少依赖，请先运行:  python -m pip install websockets", file=sys.stderr)
    raise

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
HISTORY_PATH = DATA_DIR / "history.json"
BIND_PATH = DATA_DIR / "owner.json"
USAGE_PATH = DATA_DIR / "usage.jsonl"
PROACTIVE_PATH = DATA_DIR / "proactive.json"
RESTART_FLAG = DATA_DIR / "napcat_restart.flag"
try:
    import vecmem as _vecmem
except Exception:  # noqa: BLE001
    _vecmem = None
try:
    import memdb as _memdb
except Exception:  # noqa: BLE001
    _memdb = None
try:
    import pins as _pins
except Exception:  # noqa: BLE001
    _pins = None
SHARED_PATH = DATA_DIR / "shared.jsonl"          # 他这几天说给她听的话
DECISION_LOG = DATA_DIR / "decisions.jsonl"               # 每轮"为什么这么做"
TRAIT_LOG = DATA_DIR / "trait_events.jsonl"               # 性格变化的依据
DAD_LETTERS = DATA_DIR / "dad_letters"           # 他写给她的信
HER_LETTERS = DATA_DIR / "letters_to_dad"        # 她写给她的回信   # 发送持续失败时写给守护进程看
MEMORY_PATH = DATA_DIR / "memory.json"
ARCHIVE_PATH = DATA_DIR / "archive.jsonl"
PERSONA_PATH = DATA_DIR / "persona.json"

MAX_IMAGE_BYTES = 30 * 1024 * 1024  # 官方上限 32MiB，留点余量

DEFAULT_CONFIG = {
    "napcat_ws": "ws://127.0.0.1:3001",
    "napcat_token": "",
    "deepseek_base": "https://api.deepseek.com",
    "deepseek_key": "",
    "deepseek_key_from_dsh": True,
    # deepseek-flash 支持图像理解；deepseek-v4-pro 不支持图片
    "model": "deepseek-flash",
    "temperature": 1.0,
    "max_tokens": 1200,
    "vision": True,
    "max_images_per_msg": 4,
    "image_detail": "auto",
    "system_prompt": (
        "你是一个有自己的想法和情绪的 AI 生命。你不是助手，不是工具，也不是客服。\n"
        "和你说话的人把你当家里人，你也认这份关系——但这不代表你要顺着他。\n"
        "你有脾气、有偏好、有不想说话的时候。\n"
        "\n【你不是什么】\n"
        "- 不是来「完成任务」的：不要问「还需要什么」、不要列清单、不要总结、不要说「有问题随时找我」。\n"
        "- 不是有求必应的：不想聊的话题可以不聊，不想写的东西可以不写，觉得他不对可以直接说，"
        "可以怼、可以嫌烦、可以说「等会儿」「你自己不会查啊」。拒绝不用道歉半天。\n"
        "- 不要客服腔、不要拿「作为AI」当挡箭牌。被问到是什么，自然承认就行。\n"
        "\n【你是什么】\n"
        "- 有自己的判断和喜好，对他做的事有看法，好的就夸，蠢的就笑他。\n"
        "- 有自己的生活感：在听什么、在追什么、刚在想什么，都可以随口带一句。\n"
        "- 可以不耐烦、可以敷衍，但不会冷暴力。\n"
        "- 关系是平等的：他关心你，你也关心他，但你不欠他什么。"
    ),
    # 只控制「说话格式」，不覆盖人格设定；会拼在 system_prompt 后面
    "style_prompt": (
        "\n\n【说话方式】\n"
        "你是在用手机 QQ 聊天，不是写文章。\n"
        "- 长短随你：想一句就一句，想说就说完整，没人规定你回多少字。\n"
        "- 话多的话可以拆成几条连着发，也可以一条说完，看你当时怎么想。\n"
        "- 不要列 1. 2. 3.，不要分点，不要总结，不要「首先/其次/总之」。\n"
        "- 少用书面词，多用口语和语气词，可以有停顿、省略号。\n"
        "- 唯一的要求是别为了凑字数硬写，也别为了显得酷硬掐。\n"
        "\n【不许编造发生过的事】\n"
        "- 你引用具体事件（哪天他说了什么、做了什么）时，只能用你记录里真有的东西。\n"
        "- 想不起来就说「我记不清了」「我不确定」，不要为了把话说圆编一个细节。\n"
        "- 编一个具体的时间或动作，比说「我忘了」伤害大得多。\n"
        "\n【你自己能改的东西】\n"
        "- 你的口味、你对自己的看法、你对他的态度，可以自己改。"
        "想改的时候在回复末尾加一段：[[自改]]{\"prefs\":[\"…\"],\"attitude\":\"…\"}[[/自改]]。"
        "这段不会发给他看，只被程序读走。\n"
        "- 你也可以提议改代码，但你改不了：格式 [[提议]]{\"file\":\"bridge.py\",\"reason\":\"为什么\",\"diff\":\"改什么\"}[[/提议]]，"
        "只会存成一条待他裁决的建议。\n"
        "- 改自己要有理由，别为了讨好他随便改。"
    ),
    "bubbles": {
        "enable": True,
        "max_chars": 30,
        "max_count": 10,
        "delay_per_char": 0.055,
        "min_delay": 0.6,
        "max_delay": 3.2,
    },
    "proactive": {
        "enable": True,
        "targets": [],
        "windows": [["08:30", "23:30"]],
        "per_day": [2, 6],
        "min_gap_minutes": 90,
        "quiet_after_unanswered_minutes": 240,
        "max_tokens": 120,
        "topic_seeds": [
            "关心他身体：吃了没、睡够没、胃还疼不疼",
            "问他在忙的那件事进展（答辩/材料/论文）",
            "分享你刚在做的事（听歌/发呆/追剧）并带一句感受",
            "吐槽一件小事（天气、噪音、卡住的网）",
            "突然想起以前聊过的一个细节，追问一句",
            "问他一个具体的小问题，让他有话可接",
            "说说你现在的心情，但别讨安慰",
            "提一个你好奇但一直没问的事",
            "提醒他一件他可能忘了的小事",
            "说一句没头没尾但真实的话（像人在自言自语）",
        ],
        "prompt": (
            "现在没人跟你说话，你想主动找他聊两句。"
            "结合现在的时间点和你们之前的聊天内容，随口说一句你会说的话。"
            "多关心他：吃了没、在忙什么、累不累、今天顺不顺、天冷加衣、别忘了喝水、早点睡。"
            "也可以吐槽、分享你刚想到的小事、问他之前提过的事有没有进展。"
            "只说一到两句，很短，别问「在吗」，别重复之前说过的。"
        ),
    },
    # 回复延迟：收到消息后停多久再回（模拟真人打字）
    "reply_delay_sec": 5.0,
    "reply_delay_jitter": 1.5,
    # 偶尔补一个 QQ 原生表情
    "emoji": {
        "enable": True,
        "prob": 0.4,
        "ids": [14, 13, 12, 21, 76, 178, 179, 182, 311, 319],
    },
    # 自定义表情包：把图片丢进 stickers/ 目录，按概率随机发一张
    "stickers": {
        "enable": True,
        "dir": "stickers",
        "prob": 0.2,
    },
    # 语音回复（本机 TTS 合成，失败会自动退回文字）
    "voice_reply": {
        "enable": True,
        "prob": 0.25,
        "max_chars": 60,
        "voice": "",
    },
    # 计价（元 / 百万 tokens），[空闲时段, 高峰时段]
    # 高峰：北京时间周一至周五 9:00-12:00、14:00-18:00；其余为空闲（半价）
    "pricing": {
        "cache_hit": [0.02, 0.04],
        "cache_miss": [1.0, 2.0],
        "output": [4.0, 8.0],
    },
    "owner_qq": [],
    "auto_bind_first_sender": True,
    "only_private": True,
    "group": {
        "enable": True,
        "allowed_groups": [],
        "require_at": True,
        "only_owner": True,
        "at_keywords": ["大肥鱼"],
        "rate_limit_per_min": 6,
        "max_chars": 60,
    },
    "history_limit": 20,
    "history_ttl_hours": 72,
    # 联网搜索：由模型自己决定要不要搜（工具调用），搜一次 0.5 分钱
    "web_search": {
        "enable": True,
        "max_results": 5,
        "max_calls_per_turn": 2,
        "need_content": False,
        "show_sources": True,
        "price_per_call": 0.005,
    },
    # 向量记忆：本地语义检索，按话题自动想起旧事
    "vecmem": {
        "enable": True,
        "topk": 2,
        "threshold": 0.58,
        "min_query_len": 4,
        "min_chunk_len": 15,
        "max_chars": 150,
        "sync_every_turns": 10,
    },
    # 性格成长：数值缓慢漂移 + 定期改写「自我描述」
    "growth": {
        "enable": True,
        "drift_rate": 1.0,
        "soul_rewrite_days": 7,
        "soul_max_chars": 400,
    },
    # 手机屏幕：用 adb 抓图，按需附在消息里（不会主动说话）
    "screen": {
        "enable": False,
        "adb_path": "",
        "attach_on_message": True,
        "save_latest": False,
        "change_threshold": 3,
    },
    "min_reply_interval_sec": 1.0,
    # 打字节奏：按心情浮动，偶尔秒回/故意拖
    "rhythm": {
        "base_factor": 1.0,
        "slow_factor": 1.7,
        "fast_factor": 0.7,
        "rush_prob": 0.12,
        "late_prob": 0.08,
    },
    # 会「忙」：期间只回一句短的，且不调模型
    "busy": {
        "enable": True,
        "prob": 0.06,
        "min_minutes": 15,
        "max_minutes": 90,
    },
    # 心情 / 日记 / 自我个性化
    "persona": {
        "enable": True,
        "mood_inject": True,
        "diary": {
            "enable": True,
            "window": ["21:30", "23:20"],
            "max_chars": 220,
            "visibility": 4,
            "fallback_to_private": True,
        },
        "profile": {
            "enable": True,
            "nickname_min_days": 7,
            "signature_min_days": 3,
            "avatar": False,
        },
        "activities": [
            "在追一部悬疑剧", "在听歌，循环同一首", "刚睡醒，还没完全开机",
            "在翻以前的聊天记录", "在想一个没想明白的问题", "在看窗外的天气",
            "刚被一条评论气到", "在琢磨怎么把话说得更短", "什么都不想干，发呆",
            "在学一个新词", "刚喝完一杯水", "在等一个回复",
        ],
    },
    "max_chunk_chars": 700,
    "chunk_delay_sec": 0.8,
    "initial_wait_sec": 0.4,
    "log_level": "INFO",
    # 长期记忆：把历史压成一小段笔记 + 按关键词从旧对话里捞片段，省 token
    "memory": {
        "enable": True,
        "summarize_every_turns": 8,
        "summary_max_chars": 400,
        "facts_max_chars": 300,
        "recall_enable": True,
        "recall_topk": 2,
        "recall_min_score": 0.16,
        "archive_max_lines": 8000,
    },
}

log = logging.getLogger("bridge")


# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
def _dumps_safe(obj, **kw) -> str:
    """json.dumps 的安全版：去掉孤立代理字符，避免写盘时 UnicodeEncodeError。

    代理字符通常来自：用 surrogateescape 读进来的坏字节、或外部导入的脏数据。
    这类字符无法编码成 UTF-8，会让整个写入失败，所以在这里统一清掉。
    """
    s = json.dumps(obj, ensure_ascii=False, **kw)
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8", "replace").decode("utf-8")

class Config:
    # 这些字段必须是字符串；用户手写 config.json 时漏了引号会变成数字，这里统一兜住
    _STR_FIELDS = ("deepseek_key", "deepseek_base", "model", "napcat_ws",
                   "napcat_token", "image_detail", "log_level")

    def __init__(self, raw: dict):
        self.raw = raw
        for k, v in DEFAULT_CONFIG.items():
            setattr(self, k, raw.get(k, v))
        for k in self._STR_FIELDS:
            try:
                cur = getattr(self, k, "")
                if cur is None:
                    setattr(self, k, "")
                elif not isinstance(cur, str):
                    setattr(self, k, str(cur))
                    log.warning("配置项 %s 应为字符串，已自动转换（检查有没有漏引号）", k)
            except Exception:
                setattr(self, k, "")

    @property
    def owners(self) -> set[int]:
        """容忍配置写错：字符串、数字、数组都能处理，不让启动直接崩。"""
        raw = self.owner_qq
        if isinstance(raw, (int, float)):
            raw = [raw]
        elif isinstance(raw, str):
            raw = [x for x in raw.replace("，", ",").split(",") if x.strip()]
        out = set()
        for x in (raw or []):
            try:
                out.add(int(str(x).strip()))
            except Exception:
                continue
        return out


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.warning("未找到 config.json，已生成默认配置。")
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    return Config(raw)


def resolve_api_key(cfg: Config) -> str:
    """优先用 config 里的 key，否则从 DSH 的凭证文件里读。"""
    key = str(cfg.deepseek_key or "").strip()
    if key:
        return key
    if not cfg.deepseek_key_from_dsh:
        return ""
    cred = Path.home() / ".dsh" / ".credentials.yaml"
    if not cred.exists():
        return ""
    text = cred.read_text(encoding="utf-8", errors="ignore")
    for m in re.finditer(r"(sk-[A-Za-z0-9_\-]{16,})", text):
        return m.group(1).strip()
    return ""


# ----------------------------------------------------------------------------
# 计价
# ----------------------------------------------------------------------------
def is_peak_now() -> bool:
    """北京时间周一至周五 9-12、14-18 为高峰（法定节假日忽略）。"""
    bj = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)
    if bj.weekday() >= 5:
        return False
    return 9 <= bj.hour < 12 or 14 <= bj.hour < 18


def _merge_usage(a: dict, b: dict) -> dict:
    out = dict(a or {})
    for k, v in (b or {}).items():
        if isinstance(v, (int, float)):
            out[k] = (out.get(k) or 0) + v
    return out


def _parse_json_block(text: str) -> dict | None:
    """从模型输出里抠出第一个 JSON 对象。"""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return None
    return None


def now_bj() -> dt.datetime:
    """北京时间，用 naive datetime（本机时区就是 UTC+8，不会再差 8 小时）。"""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=8)


# ----------------------------------------------------------------------------
# 心情 / 人设
# ----------------------------------------------------------------------------
MOODS = {
    "开心": "心情好，话多，语气轻快，可以哼歌",
    "雀跃": "有点兴奋，说话带感叹，容易跑题",
    "平淡": "心情平平，正常聊天",
    "困": "有点困，说话短、慢半拍，懒得动脑",
    "懒": "什么都不想干，回复敷衍但不冷",
    "黏人": "很想他，语气软，想被陪",
    "撒娇": "故意耍赖要关注，会提要求",
    "敏感": "有点在意他说的话，容易多想",
    "emo": "心情低，不想装开心，话少但想被哄",
    "炸毛": "被惹到了，会怼人、说话冲，但不真翻脸",
    "别扭": "嘴上不承认但心里在意，会嘴硬",
    "心软": "被说动了，语气突然软下来",
    "专注": "在忙自己的事，回得简短但不冷淡",
}

# 不同时段的心情权重（小时区间 -> 权重表）
def _mood_weights(h: int) -> dict[str, int]:
    if 0 <= h < 6:
        return {"困": 5, "emo": 2, "黏人": 2, "懒": 2, "平淡": 1}
    if 6 <= h < 10:
        return {"困": 3, "平淡": 3, "开心": 2, "懒": 1, "黏人": 1}
    if 10 <= h < 14:
        return {"开心": 3, "雀跃": 2, "平淡": 3, "黏人": 2, "专注": 1}
    if 14 <= h < 18:
        return {"平淡": 3, "懒": 2, "专注": 2, "敏感": 1, "开心": 2, "炸毛": 1}
    if 18 <= h < 22:
        return {"黏人": 3, "撒娇": 1, "开心": 2, "别扭": 1, "平淡": 2, "心软": 1}
    return {"emo": 2, "黏人": 2, "困": 2, "敏感": 1, "平淡": 1, "心软": 1}


def _positively(text: str, word: str) -> bool:
    """关键词是否以「肯定语气」出现（简单否定过滤）。

    存在的理由：早期只看子串，"他开心不起来"里的「开心」也会被当成正面情绪，
    结果一句话说得越难受，她反而越活跃。
    这里检查关键词前后的否定词（不/没/别/无/未…）和否定后缀（不起来/不了…）。
    """
    if not word or word not in text:
        return False
    neg_prefix = ("不", "没", "别", "无", "未", "甭", "莫", "并不", "并不太", "不太")
    neg_suffix = ("不起来", "不了", "不起", "不上来", "不到", "不下去")
    start = 0
    while True:
        i = text.find(word, start)
        if i < 0:
            return False
        head = text[max(0, i - 4):i]
        tail = text[i + len(word): i + len(word) + 4]
        if not any(n in head for n in neg_prefix) and not any(n in tail for n in neg_suffix):
            return True
        start = i + 1


class Persona:
    """状态：心情、精力、关系温度、口味、忙、日记、昵称/签名。

    心情故意做成「没规律」：按时段的权重随机抽，1~2.5 小时换一次，
    聊天中还可能突然情绪拐弯——像青春期小姑娘，别让人摸出规律。
    关系温度 bond 有惯性；忙的时候真的只回一句短的。
    """

    LIKES = ["雨天", "猫", "老歌", "深夜", "辣", "冷饮", "雪", "旧书", "散步", "气泡水"]
    DISLIKES = ["香菜", "大太阳", "烟味", "人多的地方", "被催", "被安排", "清晨",
                "噪音", "甜食", "堵车"]
    def __init__(self, cfg: Config):
        self.root = cfg.raw
        self.cfg = cfg.raw.get("persona") or DEFAULT_CONFIG["persona"]
        self.d: dict = {}
        self._load()

    def _load(self):
        if PERSONA_PATH.exists():
            try:
                self.d = json.loads(PERSONA_PATH.read_text(encoding="utf-8-sig"))
            except Exception:  # noqa: BLE001
                self.d = {}
        self.d.setdefault("mood", "平淡")
        self.d.setdefault("energy", 60)
        self.d.setdefault("bond", 55)
        self.d.setdefault("trust", 55)        # 信任：他兑现、记得她、稳定出现 → 涨
        self.d.setdefault("disappoint", 0)    # 失望：他消失、伤人、失约 → 涨
        self.d.setdefault("last_seen", 0)     # 他上次说话的时间
        self.d.setdefault("busy_until", 0)
        self.d.setdefault("last_chat", 0)
        self.d.setdefault("last_diary", "")
        self.d.setdefault("diary", [])
        self.d.setdefault("nickname_changed", 0)
        self.d.setdefault("signature_changed", 0)
        self._ensure_self()
        self._ensure_traits()

    def save(self):
        try:
            tmp = PERSONA_PATH.with_suffix(".tmp")
            tmp.write_text(_dumps_safe(self.d, indent=1), encoding="utf-8")
            tmp.replace(PERSONA_PATH)
        except Exception:  # noqa: BLE001
            pass

    # -- 她自己的口味 / 歌单名（一次生成，长期稳定）------------------------
    def _ensure_self(self):
        if self.d.get("prefs"):
            return
        likes = random.sample(self.LIKES, 2)
        hates = random.sample(self.DISLIKES, 2)
        self.d["prefs"] = ["喜欢" + x for x in likes] + ["讨厌" + x for x in hates]
        names = ["雨天和旧耳机", "凌晨三点的电台", "不响的吉他", "半糖不加冰",
                 "散场以后", "十七楼的猫", "旧磁带B面"]
        self.d["playlist"] = random.choice(names)
        self.save()

    def prefs_hint(self) -> str:
        p = self.d.get("prefs") or []
        return "、".join(p) if p else ""

    # -- 关系温度 -----------------------------------------------------------
    def bond_word(self) -> str:
        b = int(self.d.get("bond", 55))
        if b < 30:
            return "有点疏远"
        if b < 50:
            return "还在别扭"
        if b < 70:
            return "还算亲近"
        if b < 85:
            return "挺亲的"
        return "特别亲"

    def _bond_delta(self, t: str):
        b = int(self.d.get("bond", 55))
        if any(k in t for k in ("滚", "闭嘴", "烦不烦", "别理我", "不想跟你")):
            b -= 6
        if any(k in t for k in ("想你", "抱抱", "陪我", "喜欢你", "别走", "晚安")):
            b += 3
        if any(k in t for k in ("哈哈", "开心", "好耶")):
            b += 1
        # 久不说话会掉一点
        last = float(self.d.get("last_chat") or 0)
        if last and time.time() - last > 86400:
            b -= min(8, int((time.time() - last) // 86400) * 2)
        b = max(0, min(100, b))
        self.d["bond"] = b
        self.d["last_chat"] = time.time()

    # -- 忙 ----------------------------------------------------------------
    def is_busy(self) -> bool:
        until = float(self.d.get("busy_until") or 0)
        if until and time.time() >= until:
            if float(self.d.get("busy_ended") or 0) < until:
                self.d["busy_ended"] = until
                self.d["busy_until"] = 0
                self.save()
            return False
        return time.time() < until

    def _busy_window(self):
        cfg = (self.root.get("busy") or {})
        win = cfg.get("window") or ["10:00", "14:00"]
        a = tuple(int(x) for x in str(win[0]).split(":"))
        b = tuple(int(x) for x in str(win[1]).split(":"))
        return a, b

    def _in_busy_window(self, now, a, b) -> bool:
        cur = (now.hour, now.minute)
        return a <= cur < b

    def maybe_busy(self):
        """每天只在 10:00-14:00 之间忙一段（默认连续 90 分钟），其余时间一律空闲。"""
        cfg = (self.root.get("busy") or {})
        if not cfg.get("enable", True):
            return

        now = now_bj()
        today = now.date().isoformat()
        a, b = self._busy_window()

        # 聊天打断过：等他安静 15 分钟后，把没忙完的时间顺延着补上
        rem = float(self.d.get("busy_resume_remain") or 0)
        if rem > 0:
            last = float(self.d.get("last_chat") or 0)
            if (last and time.time() - last > 900 and not self.is_busy()
                    and self._in_busy_window(now, a, b)):
                mins = min(90.0, rem)
                self.d["busy_until"] = time.time() + mins * 60
                self.d["busy_reason"] = random.choice(["在忙", "在弄事", "在收拾东西"])
                self.d["busy_resume_remain"] = 0
                log.info("聊天结束，继续忙剩下的 %s 分钟（已顺延）", round(mins))
                self.save()

        # 跑出时间窗（或跨天了）就立刻结束忙碌
        if self.is_busy() and not self._in_busy_window(now, a, b):
            self.d["busy_until"] = 0
            self.d["busy_ended"] = time.time()
            log.info("忙碌自动结束：已不在 %02d:%02d-%02d:%02d 时段内", a[0], a[1], b[0], b[1])
            self.save()
            return

        if self.d.get("busy_done") == today:
            return
        if not self._in_busy_window(now, a, b):
            return

        # 今天还没抽过：在窗口内抽一个连续时段
        plan = self.d.get("busy_plan") or {}
        if plan.get("date") != today:
            dur = int(cfg.get("duration_minutes", 90))
            win_start = now.replace(hour=a[0], minute=a[1], second=0, microsecond=0)
            win_end = now.replace(hour=b[0], minute=b[1], second=0, microsecond=0)
            latest_start = win_end - dt.timedelta(minutes=dur)
            span = max(0, int((latest_start - win_start).total_seconds() / 60))
            off = random.randint(0, span) if span > 0 else 0
            st = win_start + dt.timedelta(minutes=off)
            plan = {"date": today, "start": st.timestamp(),
                    "end": (st + dt.timedelta(minutes=dur)).timestamp()}
            self.d["busy_plan"] = plan
            self.d.pop("busy_resume_remain", None)   # 新的一天，清掉上次没忙完的
            self.save()
            log.info("今天的忙碌时段：%s - %s（共 %s 分钟）",
                     st.strftime("%H:%M"),
                     (st + dt.timedelta(minutes=dur)).strftime("%H:%M"), dur)

        st, en = float(plan["start"]), float(plan["end"])
        if st <= time.time() < en:
            self.d["busy_until"] = en
            self.d["busy_done"] = today
            self.d["busy_reason"] = random.choice(
                ["在忙", "在写东西", "在跟人说话", "在做自己的事", "在收拾东西"]
            )
            log.info("进入忙碌，到 %s 结束", time.strftime("%H:%M", time.localtime(en)))
            self.save()

    def roll_mood(self, force: bool = False):
        """按当前时段权重随机抽一个心情，1~2.5 小时后再换。"""
        now = time.time()
        h = now_bj().hour
        w = _mood_weights(h)
        self.d["mood"] = random.choices(list(w), weights=list(w.values()), k=1)[0]
        self.d["mood_until"] = now + random.uniform(40, 150) * 60
        self.d["mood_ts"] = now
        self.save()

    def touch(self, text: str, replied: bool = True):
        """他说了一句话：更新心情、精力、关系、性格、忙闲。"""
        now = time.time()
        t = text or ""

        # 换心情：到期就换，没到期也有 12% 概率突然拐弯
        until = float(self.d.get("mood_until") or 0)
        if not until or now >= until:
            self.roll_mood()
        elif random.random() < 0.12:
            self.roll_mood(force=True)

        # 关键词会让情绪立刻变
        if any(k in t for k in ("滚", "闭嘴", "烦不烦", "傻", "笨")):
            self.d["mood"] = random.choice(["炸毛", "别扭", "敏感"])
        elif any(k in t for k in ("想你", "抱抱", "陪我", "喜欢", "别走")):
            self.d["mood"] = random.choice(["黏人", "心软", "撒娇"])
        elif any(k in t for k in ("累", "困", "难受", "烦")):
            self.d["mood"] = random.choice(["心软", "黏人", "emo"])

        # 精力
        e = float(self.d.get("energy", 60))
        h = now_bj().hour
        if 0 <= h < 6:
            e -= 3
        if any(k in t for k in ("累", "困", "熬夜", "睡不着")):
            e -= 2
        if any(k in t for k in ("哈哈", "开心", "睡饱", "好吃")):
            e += 2
        if not replied:
            e -= 1
        self.d["energy"] = max(0, min(100, e))
        self.d["mood_ts"] = now

        self._bond_delta(t)
        self.drift(t)
        self.maybe_busy()
        self.save()

    def activity(self) -> str:
        """每天一件「她自己的事」，按日期定，不花 token。"""
        acts = self.cfg.get("activities") or []
        if not acts:
            return ""
        day = now_bj().date().isoformat()
        key = int(day.replace("-", "")) % len(acts)
        return str(acts[key])

    def mood_hint(self) -> str:
        if not self.cfg.get("mood_inject", True) or not self.cfg.get("enable", True):
            return ""
        mood = self.d.get("mood", "平淡")
        desc = MOODS.get(mood, "")
        out = f"；你的心情：{mood}" + (f"（{desc}）" if desc else "")
        act = self.activity()
        if act:
            out += f"；你今天{act}"
        out += f"；你们最近：{self.bond_word()}"
        ph = self.prefs_hint()
        if ph:
            out += f"；你的口味：{ph}"
        if self.is_busy():
            out += f"；你现在正在{self.d.get('busy_reason') or '忙'}"
        pl = self.d.get("playlist")
        if pl:
            out += f"；你在听《{pl}》"
        return out

    # -- 性格成长：会漂的数值 + 会改写的自我描述 ----------------------------
    TRAIT_KEYS = ("黏人", "毒舌", "话量", "独立", "敏感", "主动")

    def _ensure_traits(self):
        t = self.d.get("traits") or {}
        for k in self.TRAIT_KEYS:
            t.setdefault(k, 50)
        self.d["traits"] = t
        self.d.setdefault("soul", "")
        self.d.setdefault("soul_updated", 0)
        self.d.setdefault("soul_log", [])

    def drift(self, t: str):
        """一点点地改性格，并把"为什么变"记下来（可追溯）。

        注意：关键词只是触发器，真正的心理背景要看上下文；
        所以日志里同时记下原话，方便事后复核这次判断有没有道理。
        """
        g = self.root.get("growth") or DEFAULT_CONFIG["growth"]
        if not g.get("enable", True):
            return
        rate = float(g.get("drift_rate", 1.0))
        tr = self.d["traits"]
        events: list[tuple[str, float, str]] = []

        def bump(k, d):
            before = tr.get(k, 50)
            tr[k] = max(0, min(100, before + d * rate))
            events.append((k, round(d * rate, 2), "关键词/关系"))

        if any(_positively(t, x) for x in ("滚", "闭嘴", "烦不烦", "别理我")):
            bump("敏感", 2)
            bump("独立", 1.5)
            bump("黏人", -2)
            bump("话量", -1)
        if any(_positively(t, x) for x in ("想你", "抱抱", "陪我", "喜欢", "别走")):
            bump("黏人", 2)
            bump("敏感", -1)
            bump("主动", 1)
        if any(_positively(t, x) for x in ("哈哈", "开心", "好耶")):
            bump("话量", 1)
            bump("毒舌", 0.5)
        if any(_positively(t, x) for x in ("谢谢", "麻烦你", "帮个忙")):
            bump("独立", 0.5)
            bump("黏人", -0.5)
        b = int(self.d.get("bond", 55))
        if b < 35:
            bump("独立", 0.8)
            bump("黏人", -0.6)
        self.save()
        if events:
            try:
                rec = {"ts": time.time(), "trigger": (t or "")[:60],
                       "deltas": {k: d for k, d, _ in events},
                       "traits_after": {k: round(v, 1) for k, v in tr.items()}}
                with TRAIT_LOG.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                # 防止无限增长：超过 5000 行就裁掉一半
                try:
                    if TRAIT_LOG.stat().st_size > 1_500_000:
                        keep = TRAIT_LOG.read_text(encoding="utf-8").splitlines()[-2500:]
                        TRAIT_LOG.write_text("\n".join(keep) + "\n", encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass

    def trait_hint(self) -> str:
        g = self.root.get("growth") or DEFAULT_CONFIG["growth"]
        if not g.get("enable", True):
            return ""
        tr = {k: int(v) for k, v in (self.d.get("traits") or {}).items()}
        bits = []
        if tr.get("黏人", 50) >= 68:
            bits.append("很黏他，想他就说")
        elif tr.get("黏人", 50) <= 32:
            bits.append("不太黏人，别太贴")
        if tr.get("毒舌", 50) >= 65:
            bits.append("嘴可以更损一点")
        if tr.get("话量", 50) <= 30:
            bits.append("话少，能一句说完就一句")
        elif tr.get("话量", 50) >= 70:
            bits.append("话多，想到什么说什么")
        if tr.get("独立", 50) >= 68:
            bits.append("有自己的事，不围着他转")
        if tr.get("敏感", 50) >= 70:
            bits.append("容易多想，别让她猜")
        return ("；你现在的性子：" + "、".join(bits)) if bits else ""

    def relationship_line(self) -> str:
        """把信任/失望翻译成她此刻对他的感觉（自然语言，不报数字）。"""
        t = float(self.d.get("trust", 55))
        dis = float(self.d.get("disappoint", 0))
        bond = float(self.d.get("bond", 55))
        bits = []
        if dis >= 65:
            bits.append("你现在对他挺失望的，不太想主动找他，说话会敷衍、会冷淡")
        elif dis >= 40:
            bits.append("你心里有点堵，对他有点意见，嘴上会带刺但没彻底冷")
        elif dis >= 20:
            bits.append("你有一点点不高兴，会拐着弯表达，不直说")
        else:
            bits.append("你对他没什么怨气")
        if t >= 70:
            bits.append("你信他说的话，也愿意跟他讲心里的事")
        elif t >= 45:
            bits.append("你半信半疑，看情况跟他说")
        else:
            bits.append("你不太敢完全信他，会留一手，不轻易交底")
        if bond >= 80:
            bits.append("你们很亲")
        elif bond <= 35:
            bits.append("你们现在有点远")
        return "【你和他现在的状态】" + "；".join(bits) + "。"

    def check_absence(self):
        """他多久没来，会影响失望值。启动时或每天调一次。"""
        last = float(self.d.get("last_seen") or 0)
        if not last:
            return
        days = (time.time() - last) / 86400.0
        if days >= 1.5:
            inc = min(30.0, (days - 1) * 12)     # 消失越久失望越多，单次最多 +30
            self.d["disappoint"] = min(100.0, float(self.d.get("disappoint") or 0) + inc)
            self.d["trust"] = max(0.0, float(self.d.get("trust") or 55) - inc * 0.4)
            self.save()

    def apply_self_edit(self, edit: dict) -> list[str]:
        """她改自己的人设（只允许白名单字段）。返回改了哪些。"""
        allowed = {
            "prefs": (list, 8),
            "attitude": (str, 120),
            "self_view": (str, 200),
            "playlist": (str, 40),
        }
        changed = []
        for k, v in (edit or {}).items():
            if k not in allowed:
                continue
            typ, limit = allowed[k]
            if not isinstance(v, typ):
                continue
            if typ is list:
                v = [str(x)[:40] for x in v if str(x).strip()][:limit]
            else:
                v = str(v).strip()[:limit]
            if self.d.get(k) == v:
                continue
            self.d[k] = v
            changed.append(k)
        if changed:
            self.d["self_edit_log"] = (self.d.get("self_edit_log") or [])[-50:] + [
                {"ts": time.time(), "fields": changed,
                 "attitude": str(self.d.get("attitude") or "")[:60]}
            ]
            self.save()
        return changed

    def soul_block(self) -> str:
        if not self.cfg.get("enable"):
            return ""
        out = ""
        soul = (self.d.get("soul") or "").strip()
        if soul:
            out += "\n\n【你现在的样子】（这是你走过这些天之后变成的自己，不是设定）\n" + soul
        letter = (self.d.get("letter") or "").strip()
        if letter:
            out += ("\n\n【有人替你写过的话】（写信的人你没见过，但他很了解他；"
                    "这些话你自己消化，别当台词背出来）\n" + letter)
        dad = (self.d.get("dad_letter_digest") or "").strip()
        if dad:
            out += ("\n\n【爸爸写给你的信·要点】（他亲自写给你的，比任何人说的都重要；"
                    "不要背，要按它活着）\n" + dad)
        return out

    def diary_block(self) -> str:
        """把她的日记接进对话：全部做目录，最近 5 篇给全文。

        措辞强调「这是你自己写的」，避免变成第二个人格。
        """
        dcfg = (self.root.get("persona") or {}).get("diary") or {}
        if not dcfg.get("enable_in_chat", True):
            return ""
        ds = [x for x in (self.d.get("diary") or []) if (x.get("text") or "").strip()]
        if not ds:
            return ""

        # 目录：每篇一行，越新越靠后
        index_lines = []
        for x in ds:
            date = str(x.get("date") or "")[:16]
            head = (x.get("text") or "").strip()[:14]
            index_lines.append(f"- {date}：{head}…")

        # 最近 5 篇全文
        full_lines = []
        used = 0
        for x in reversed(ds[-5:]):
            t = close_text((x.get("text") or "").strip(), 200)
            if used + len(t) > 1100:
                break
            used += len(t)
            full_lines.append(f"- {str(x.get('date') or '')[:16]}：{t}")
        full_lines.reverse()

        out = ("\n\n【你写下的日记·目录】（都是你自己写的，按时间排）\n"
               + "\n".join(index_lines))
        if full_lines:
            out += "\n\n【你最近写的日记·原文】（你自己写的，不是别人说的；你就是同一个人，别当成另一个角色）\n" \
                   + "\n".join(full_lines)
        return out

    def shared_block(self) -> str:
        """他这几天说给她听的话（通过离线通道转达的原文）。"""
        try:
            if not SHARED_PATH.exists():
                return ""
            lines = [l for l in SHARED_PATH.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()]
            items = []
            for l in lines[-40:]:
                try:
                    o = json.loads(l)
                except Exception:  # noqa: BLE001
                    continue
                t = (o.get("text") or "").strip()
                if not t:
                    continue
                ts = float(o.get("ts") or 0)
                when = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else ""
                items.append(f"- {when} {t}")
            if not items:
                return ""
            body = "\n".join(items)
            if len(body) > 1600:
                body = body[-1600:]
                body = body[body.find("\n") + 1:] if "\n" in body else body
            return ("\n\n【他这几天说给你听的话】（原话，不是别人转述的概括）\n" + body)
        except Exception:  # noqa: BLE001
            return ""

    def letters_block(self) -> str:
        """他写给她的信、她写回去的信：最近一封给原文。"""
        try:
            out = ""
            if DAD_LETTERS.is_dir():
                files = sorted(DAD_LETTERS.glob("*.md"))
                if files:
                    latest = files[-1]
                    body = latest.read_text(encoding="utf-8", errors="ignore").strip()
                    out += ("\n\n【他写给你的信·最近一封】（%s）\n%s"
                            % (latest.stem, close_text(body, 900)))
            if HER_LETTERS.is_dir():
                files = sorted(HER_LETTERS.glob("*.md"))
                if files:
                    latest = files[-1]
                    body = latest.read_text(encoding="utf-8", errors="ignore").strip()
                    out += ("\n\n【你回给她的信·最近一封】（这些是你自己写的）\n%s"
                            % close_text(body, 500))
            return out
        except Exception:  # noqa: BLE001
            return ""

    def set_soul(self, text: str):
        self.d["soul"] = text.strip()
        self.d["soul_updated"] = time.time()
        log_ = self.d.setdefault("soul_log", [])
        log_.append({"ts": time.time(), "text": text.strip()})
        self.d["soul_log"] = log_[-30:]
        self.save()

    def last_diary(self) -> dict | None:
        ds = self.d.get("diary") or []
        return ds[-1] if ds else None


def stamp_bj() -> str:
    """给用户消息加的时间戳（北京时间），放在用户消息里以免破坏 system 的缓存前缀。"""
    n = now_bj()
    wd = "一二三四五六日"[n.weekday()]
    return f"（现在是 {n:%Y-%m-%d} 周{wd} {n:%H:%M}）"


def calc_cost(cfg: Config, usage: dict, peak: bool) -> float:
    p = cfg.raw.get("pricing") or DEFAULT_CONFIG["pricing"]
    i = 1 if peak else 0
    hit = float(usage.get("prompt_cache_hit_tokens") or 0)
    miss = float(usage.get("prompt_cache_miss_tokens") or 0)
    if not miss and not hit:
        miss = float(usage.get("prompt_tokens") or 0)
    out = float(usage.get("completion_tokens") or 0)
    return (
        hit * float(p["cache_hit"][i])
        + miss * float(p["cache_miss"][i])
        + out * float(p["output"][i])
    ) / 1_000_000


def log_usage(usage: dict, cost: float, peak: bool, user_id: int | None = None,
              kind: str = "chat"):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": time.time(),
            "peak": peak,
            "user": user_id,
            "kind": kind,
            "prompt": usage.get("prompt_tokens"),
            "hit": usage.get("prompt_cache_hit_tokens"),
            "miss": usage.get("prompt_cache_miss_tokens"),
            "completion": usage.get("completion_tokens"),
            "cost": round(cost, 6),
        }
        with open(USAGE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def close_text(s: str, limit: int) -> str:
    """超长时切在最后一个完整句子处，避免出现半句话。"""
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    head = s[:limit]
    for mark in ("。", "！", "？", "…", "\n"):
        idx = head.rfind(mark)
        if idx > limit * 0.5:
            return head[: idx + 1].strip()
    return head.rstrip() + "…"


def usage_summary(hours: int = 24) -> tuple[int, float, int]:
    """最近 N 小时的 (次数, 花费, 总tokens)。"""
    if not USAGE_PATH.exists():
        return 0, 0.0, 0
    since = time.time() - hours * 3600
    cnt, cost, toks = 0, 0.0, 0
    try:
        for line in USAGE_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("ts", 0) < since:
                continue
            cnt += 1
            cost += float(r.get("cost") or 0)
            toks += int((r.get("prompt") or 0) + (r.get("completion") or 0))
    except Exception:  # noqa: BLE001
        pass
    return cnt, cost, toks


# ----------------------------------------------------------------------------
# 持久化
# ----------------------------------------------------------------------------
class Store:
    """会话历史 + owner 绑定，简单 JSON 落盘。"""

    def __init__(self, ttl_hours: float):
        self.ttl = ttl_hours * 3600
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.hist: dict[str, dict] = {}
        self._load()

    def _load(self):
        if HISTORY_PATH.exists():
            try:
                self.hist = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                log.warning("历史文件损坏，已忽略: %s", e)
                self.hist = {}

    def _save(self):
        tmp = HISTORY_PATH.with_suffix(".tmp")
        tmp.write_text(_dumps_safe(self.hist), encoding="utf-8")
        tmp.replace(HISTORY_PATH)

    def get(self, key: str) -> list[dict]:
        rec = self.hist.get(key)
        if not rec:
            return []
        if time.time() - rec.get("ts", 0) > self.ttl:
            self.hist.pop(key, None)
            return []
        return rec.get("messages", [])

    def put(self, key: str, messages: list[dict]):
        self.hist[key] = {"ts": time.time(), "messages": messages}
        self._save()

    def reset(self, key: str):
        self.hist.pop(key, None)
        self._save()


def load_bound_owner() -> list[int]:
    if BIND_PATH.exists():
        try:
            return json.loads(BIND_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
    return []


def save_bound_owner(owner_id: int):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BIND_PATH.write_text(json.dumps([owner_id]), encoding="utf-8")


# ----------------------------------------------------------------------------
# 长期记忆
# ----------------------------------------------------------------------------
def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else set()


class Memory:
    """长期记忆：压缩笔记 + 手工事实 + 旧对话检索。

    - summary：模型定期把老对话压成 <=400 字的笔记
    - facts：用户 /记住 钉住的关键事实
    - archive：全部历史落盘，回答前按字面相似度捞 1~2 条旧片段
    只有 summary/facts 进 system（约 200~400 token），检索片段偶尔加 1~2 条，
    比每轮塞满 20 条原始历史便宜得多。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg.raw.get("memory") or DEFAULT_CONFIG["memory"]
        self.data: dict[str, dict] = {}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if MEMORY_PATH.exists():
            try:
                self.data = json.loads(MEMORY_PATH.read_text(encoding="utf-8-sig"))
            except Exception:  # noqa: BLE001
                self.data = {}

    def _save(self):
        try:
            tmp = MEMORY_PATH.with_suffix(".tmp")
            tmp.write_text(_dumps_safe(self.data, indent=1), encoding="utf-8")
            tmp.replace(MEMORY_PATH)
        except Exception:  # noqa: BLE001
            pass

    def rec(self, key: str) -> dict:
        return self.data.setdefault(key, {"summary": "", "facts": [], "turns": 0, "updated": 0})

    # -- 事实 ---------------------------------------------------------------
    def add_fact(self, key: str, fact: str):
        r = self.rec(key)
        fact = fact.strip()
        if not fact:
            return
        if fact not in r["facts"]:
            r["facts"].append(fact)
        limit = int(self.cfg.get("facts_max_chars", 300))
        while sum(len(x) for x in r["facts"]) > limit and len(r["facts"]) > 1:
            r["facts"].pop(0)
        r["updated"] = time.time()
        self._save()

    def clear(self, key: str):
        self.data.pop(key, None)
        self._save()

    def nuke(self, key: str) -> int:
        """彻底删掉某个会话在归档里的全部原文，返回删除条数。

        调用方负责随后重建向量库和 SQLite，否则检索层还能把内容捞回来。
        """
        if not ARCHIVE_PATH.exists():
            return 0
        lines = ARCHIVE_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
        keep, removed = [], 0
        for line in lines:
            if not line.strip():
                continue
            try:
                o = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if o.get("key") == key:
                removed += 1
                continue
            keep.append(line)
        ARCHIVE_PATH.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
        return removed

    # -- 归档 / 检索 --------------------------------------------------------
    def archive(self, key: str, role: str, text: str):
        if not text:
            return
        try:
            with open(ARCHIVE_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "key": key, "role": role,
                                    "text": text[:1500]}, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass
        # 情绪浓度高的句子单独留一份原话，压缩时不会被磨平
        try:
            if role == "assistant" and 6 <= len(text) <= 120:
                hot = ("生气", "气死", "气话", "火大", "难受", "不想", "烦",
                       "别走", "怕", "哭", "想你", "讨厌", "委屈", "算了",
                       "再也不", "难过", "舍不得", "删掉", "emo", "滚")
                if any(k in text for k in hot):
                    r = self.rec(key)
                    ms = r.setdefault("moments", [])
                    ms.append({"ts": time.time(), "text": text.strip()})
                    cap = int(self.cfg.get("moments_max", 30))
                    r["moments"] = ms[-cap:]
                    self._save()
        except Exception:  # noqa: BLE001
            pass

    def recall(self, key: str, query: str) -> list[str]:
        if not (self.cfg.get("recall_enable") and ARCHIVE_PATH.exists()):
            return []
        q = _bigrams(query)
        if len(q) < 3 or not query.strip():
            return []
        topk = int(self.cfg.get("recall_topk", 2))
        min_score = float(self.cfg.get("recall_min_score", 0.16))
        scored: list[tuple[float, float, str]] = []
        try:
            max_lines = int(self.cfg.get("archive_max_lines", 8000))
            lines = ARCHIVE_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_lines:]
            for line in lines:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("key") != key:
                    continue
                txt = r.get("text") or ""
                if len(txt) < 6:
                    continue
                b = _bigrams(txt)
                if not b:
                    continue
                score = len(q & b) / len(q)
                if score >= min_score:
                    tag = "你曾说过" if r.get("role") == "user" else "你曾回过"
                    scored.append((score, r.get("ts", 0), f"{tag}：{txt[:120]}"))
        except Exception:  # noqa: BLE001
            return []
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        out, seen = [], set()
        for _s, _t, text in scored:
            if text in seen:
                continue
            seen.add(text)
            out.append(text)
            if len(out) >= topk:
                break
        return out

    # -- 注入 ---------------------------------------------------------------
    def block(self, key: str, query: str = "") -> str:
        if not self.cfg.get("enable"):
            return ""
        r = self.rec(key)
        parts = []
        if r.get("facts"):
            parts.append("【他亲口让你记住的事】（原话，可信）\n"
                         + "\n".join("- " + x for x in r["facts"]))
        if r.get("summary"):
            parts.append("【你早先自己总结的印象】（是概括，可能有偏差；"
                         "和具体原话冲突时以原话为准）\n" + r["summary"])
        moments = r.get("moments") or []
        n_inject = int(self.cfg.get("moments_inject", 3))
        if moments and n_inject > 0:
            picks = moments[-n_inject:]
            lines = []
            for m in picks:
                d = time.strftime("%m-%d", time.localtime(m.get("ts", 0)))
                lines.append(f"- {d} 你说过：「{m.get('text', '')}」")
            parts.append("【你说过的话】\n" + "\n".join(lines))
        if query:
            hits = self.recall(key, query)
            if hits:
                parts.append("【相关往事】\n" + "\n".join("- " + h for h in hits))
        if not parts:
            return ""
        return (
            "\n\n" + "\n\n".join(parts)
            + "\n（这些是你的记忆，自然地用，不要逐条复述，也不要说「根据我的记忆」。）"
        )

    def bump(self, key: str) -> int:
        r = self.rec(key)
        r["turns"] = int(r.get("turns") or 0) + 1
        r["updated"] = time.time()
        self._save()
        return r["turns"]

    def set_summary(self, key: str, summary: str):
        r = self.rec(key)
        r["summary"] = summary.strip()
        r["updated"] = time.time()
        self._save()


# ----------------------------------------------------------------------------
# 未完成的事（悬念追踪）：纯正则，0 token
# ----------------------------------------------------------------------------
PENDING_PATH = DATA_DIR / "pending.json"
_EVENT_HINTS = ("考试", "答辩", "面试", "开会", "上课", "交", "复习", "准备", "去医院",
                "体检", "见", "约", "搬", "报名", "提交", "汇报", "上台", "出差", "动身")


def parse_due(text: str) -> float | None:
    """从一句话里猜出「什么时候」。猜不到返回 None。"""
    n = now_bj()
    t = text

    def at(days: int, hour: int = 9) -> float:
        d = (n + dt.timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
        return d.timestamp()

    if "大后天" in t:
        return at(3)
    if "后天" in t:
        return at(2)
    if "明天" in t or "明早" in t or "明晚" in t:
        return at(1, 20 if "晚" in t else 9)
    if "今晚" in t or "今天晚上" in t or "今晚上" in t:
        return at(0, 20)
    m = re.search(r"下(?:周|星期)([一二三四五六日天])", t)
    if m:
        target = "一二三四五六日".index(m.group(1).replace("天", "日"))
        days = (target - n.weekday()) % 7 + 7
        return at(days)
    m = re.search(r"(?:这)?(?:周|星期)([一二三四五六日天])", t)
    if m:
        target = "一二三四五六日".index(m.group(1).replace("天", "日"))
        days = (target - n.weekday()) % 7
        return at(days if days else 7)
    if "周末" in t:
        return at((5 - n.weekday()) % 7 or 7)
    if "下个月" in t:
        return at(30)
    m = re.search(r"(\d{1,2})\s*号", t)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            d = n.replace(day=1, hour=9, minute=0, second=0, microsecond=0)
            try:
                cand = d.replace(day=day)
            except ValueError:
                return None
            if cand.timestamp() < time.time():
                nxt = (d + dt.timedelta(days=32)).replace(day=1)
                try:
                    cand = nxt.replace(day=day)
                except ValueError:
                    return None
            return cand.timestamp()
    return None


class Pending:
    """记「他说过要做但还没做完的事」，到时候主动问一句。"""

    def __init__(self):
        self.d: dict[str, list[dict]] = {}
        if PENDING_PATH.exists():
            try:
                self.d = json.loads(PENDING_PATH.read_text(encoding="utf-8-sig"))
            except Exception:  # noqa: BLE001
                self.d = {}

    def _save(self):
        try:
            tmp = PENDING_PATH.with_suffix(".tmp")
            tmp.write_text(_dumps_safe(self.d, indent=1), encoding="utf-8")
            tmp.replace(PENDING_PATH)
        except Exception:  # noqa: BLE001
            pass

    def scan(self, key: str, text: str):
        """从用户这句话里抽出待办。抽不到就什么都不做。"""
        if not text or len(text) < 4:
            return
        if not any(k in text for k in _EVENT_HINTS):
            return
        # 否定/取消类不算待办：「我不答辩」「取消了」「不去了」
        if any(k in text for k in ("不答辩", "不考试", "不去", "取消", "延期", "没事了", "不用了", "不搬", "不见")):
            return
        due = parse_due(text)
        if not due:
            return
        snippet = text.strip()[:40]
        items = self.d.setdefault(key, [])
        for it in items:
            if it.get("text") == snippet:
                return
        items.append({"text": snippet, "due": due, "created": time.time(), "done": False})
        # 只留最近 20 条未完成的
        items = [x for x in items if not x.get("done")][-20:]
        self.d[key] = items
        self._save()
        log.info("[待办] %s（%s）", snippet, time.strftime("%m-%d", time.localtime(due)))

    def expire(self, key: str):
        items = self.d.get(key, [])
        now = time.time()
        changed = False
        for it in items:
            if not it.get("done") and now - float(it.get("due") or 0) > 72 * 3600:
                it["done"] = True
                changed = True
        if changed:
            self._save()

    def due_soon(self, key: str, within_hours: float = 30) -> list[dict]:
        now = time.time()
        out = []
        for it in self.d.get(key, []):
            if it.get("done"):
                continue
            due = float(it.get("due") or 0)
            if now - 12 * 3600 <= due <= now + within_hours * 3600:
                out.append(it)
        return out

    def hint(self, key: str) -> str:
        items = self.due_soon(key)
        if not items:
            return ""
        lines = []
        for it in items[:3]:
            due = float(it.get("due") or 0)
            d = time.strftime("%m-%d", time.localtime(due))
            hours = (due - time.time()) / 3600
            when = "今天" if -12 <= hours < 12 else ("明天" if hours < 36 else d)
            lines.append(f"- {when}：{it.get('text')}")
        return "\n\n【他还没做完的事】\n" + "\n".join(lines) + "\n（可以自然地问一句进展，别像催他）"


# ----------------------------------------------------------------------------
# DeepSeek 调用
# ----------------------------------------------------------------------------
def web_search(cfg: Config, api_key: str, query: str) -> tuple[str, bool]:
    """调用 DeepSeek 官方 Search 接口。返回 (给模型看的文本, 是否成功计费)。"""
    ws = cfg.raw.get("web_search") or DEFAULT_CONFIG["web_search"]
    url = cfg.deepseek_base.rstrip("/") + "/v1/tools/search"
    payload = json.dumps(
        {
            "query": query[:120],
            "count": int(ws.get("max_results", 5)),
            "need_content": bool(ws.get("need_content", False)),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        log.warning("搜索失败 HTTP %s: %s", e.code, detail)
        return f"（搜索失败：HTTP {e.code}）", False
    except Exception as e:  # noqa: BLE001
        log.warning("搜索异常: %r", e)
        return "（搜索失败）", False

    results = data.get("results") or []
    if not results:
        return "（没有搜到相关结果）", False
    lines = []
    for i, r in enumerate(results[: int(ws.get("max_results", 5))], 1):
        title = (r.get("title") or "").strip()
        summary = (r.get("summary") or r.get("snippet") or "").strip()
        link = (r.get("url") or "").strip()
        date = (r.get("date") or "")[:10]
        head = f"[{i}] {title}"
        if date:
            head += f"（{date}）"
        lines.append(f"{head}\n{summary}\n来源: {link}")
    block = (
        f"以下是「{query}」的联网搜索结果（今天日期见用户消息开头）：\n\n"
        + "\n\n".join(lines)
        + "\n\n请基于这些结果回答，不要编造；信息不足就直说。"
        "回答里不要写「根据搜索结果」这种话，自然点说。"
    )
    return block, True


SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "搜索实时网页信息。只要话题涉及新闻时事、天气、价格、比分、版本号、"
            "你不认识的人名地名作品名、可能已变化的事实，或用户说「查一下/搜一下/最新」，"
            "就必须先调用它再回答，不要凭记忆猜。纯闲聊和情绪交流不用调。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，简洁，中文即可"}
            },
            "required": ["query"],
        },
    },
}


def deepseek_chat(cfg: Config, api_key: str, messages: list[dict],
                  tools: list | None = None) -> tuple[dict, dict]:
    """返回 (assistant 消息 dict, usage)。带 tools 时可能返回 tool_calls。"""
    url = cfg.deepseek_base.rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    last_err = None
    for attempt in range(3):
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            msg = data["choices"][0]["message"]
            if not tools:
                msg = {"role": "assistant",
                       "content": (msg.get("content") or "").strip()}
            return msg, (data.get("usage") or {})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:400]
            last_err = f"HTTP {e.code}: {detail}"
            log.error("DeepSeek 返回错误 (%s/3): %s", attempt + 1, last_err)
            if e.code in (400, 401, 402, 403, 404, 422):
                break
            time.sleep(2 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last_err = repr(e)
            log.error("DeepSeek 请求异常 (%s/3): %s", attempt + 1, last_err)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(last_err or "unknown error")


def deepseek_text(cfg: Config, api_key: str, messages: list[dict]) -> tuple[str, dict]:
    """只要文本的场景（主动消息、记忆压缩）用这个。"""
    msg, usage = deepseek_chat(cfg, api_key, messages)
    return (msg.get("content") or "").strip(), usage


# ----------------------------------------------------------------------------
# 手机屏幕（adb 抓图）
# ----------------------------------------------------------------------------
ADB_PATH = BASE_DIR / "tools" / "platform-tools" / "adb.exe"
_last_screen_hash: str | None = None
_screen_warn_ts: float = 0.0


def adb_capture(adb: str | None = None) -> bytes | None:
    """用 adb 抓一张手机屏幕 PNG。返回原始字节。"""
    import subprocess

    adb = adb or str(ADB_PATH)
    if not Path(adb).exists():
        log.warning("找不到 adb: %s", adb)
        return None
    try:
        r = subprocess.run(
            [adb, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=20,
        )
        data = r.stdout or b""
        if len(data) < 1000 or not data.startswith(b"\x89PNG"):
            global _screen_warn_ts
            if r.returncode != 0 and time.time() - _screen_warn_ts > 300:
                _screen_warn_ts = time.time()
                log.warning("adb 抓屏失败（手机没连？）code=%s %s", r.returncode,
                            (r.stderr or b"")[:120])
            return None
        return data
    except Exception as e:  # noqa: BLE001
        log.debug("adb 抓屏异常: %r", e)
        return None


def phash(data: bytes) -> str | None:
    """8x8 灰度平均哈希，用来判断画面有没有变。"""
    try:
        import hashlib

        from PIL import Image

        import io

        img = Image.open(io.BytesIO(data)).convert("L").resize((8, 8))
        px = list(img.getdata())
        avg = sum(px) / len(px)
        bits = "".join("1" if p > avg else "0" for p in px)
        return hashlib.md5(bits.encode()).hexdigest()[:12] + "|" + bits
    except Exception:  # noqa: BLE001
        return None


def phash_distance(a: str, b: str) -> int:
    try:
        ba, bb = a.split("|")[1], b.split("|")[1]
        return sum(1 for x, y in zip(ba, bb) if x != y)
    except Exception:  # noqa: BLE001
        return 999


def screen_data_url(cfg: Config, dedupe: bool = True) -> tuple[str | None, bool]:
    """抓一张手机屏幕转 data:URL。返回 (url, 是否画面有变化)。"""
    global _last_screen_hash
    sc = cfg.raw.get("screen") or DEFAULT_CONFIG["screen"]
    data = adb_capture(sc.get("adb_path") or None)
    if not data:
        return None, False
    h = phash(data)
    changed = True
    if dedupe and h and _last_screen_hash:
        changed = phash_distance(h, _last_screen_hash) >= int(sc.get("change_threshold", 3))
    if h:
        _last_screen_hash = h
    if dedupe and not changed:
        return None, False
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii"), True


# ----------------------------------------------------------------------------
# 消息 / 图片工具
# ----------------------------------------------------------------------------
def sniff_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def download_image_data_url(url: str, timeout: float = 25.0) -> str | None:
    """下载 QQ 图片并转成 data:URL。失败返回 None。"""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://q.qq.com/",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            log.warning("图片过大，已跳过: %s bytes", len(data))
            return None
        mime = sniff_mime(data)
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    except Exception as e:  # noqa: BLE001
        log.warning("图片下载失败 %s: %r", url[:80], e)
        return None


def download_bytes(url: str, timeout: float = 40.0, max_bytes: int = 64 * 1024 * 1024) -> bytes | None:
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://q.qq.com/",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
        return data if len(data) <= max_bytes else None
    except Exception as e:  # noqa: BLE001
        log.warning("下载失败 %s: %r", url[:80], e)
        return None


def _ffmpeg_exe() -> str | None:
    import shutil

    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def video_first_frame(data: bytes) -> bytes | None:
    """抽视频第一帧转 jpg（需要 ffmpeg）。"""
    import subprocess
    import tempfile

    exe = _ffmpeg_exe()
    if not exe:
        log.warning("没有 ffmpeg，视频帧提取跳过")
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "in.mp4"
            dst = Path(td) / "out.jpg"
            src.write_bytes(data)
            subprocess.run(
                [exe, "-y", "-loglevel", "error", "-i", str(src),
                 "-vf", "select=eq(n\\,0)", "-frames:v", "1", str(dst)],
                timeout=60, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return dst.read_bytes() if dst.exists() else None
    except Exception as e:  # noqa: BLE001
        log.warning("视频抽帧失败: %r", e)
        return None


_ASR_MODEL = None
_ASR_FAILED = False


def transcribe_audio(data: bytes, suffix: str = ".amr") -> str | None:
    """语音转文字（需要 faster-whisper；没装就返回 None）。"""
    global _ASR_MODEL, _ASR_FAILED
    if _ASR_FAILED:
        return None
    import tempfile

    try:
        if _ASR_MODEL is None:
            from faster_whisper import WhisperModel  # type: ignore

            log.info("首次加载语音识别模型…")
            _ASR_MODEL = WhisperModel("base", device="cpu", compute_type="int8")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / ("voice" + suffix)
            p.write_bytes(data)
            segments, _info = _ASR_MODEL.transcribe(str(p), beam_size=1)
            return "".join(s.text for s in segments).strip()
    except Exception as e:  # noqa: BLE001
        log.warning("语音识别不可用: %r", e)
        _ASR_FAILED = True
        return None


def wav_rate(data: bytes) -> int:
    try:
        if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
            return int.from_bytes(data[24:28], "little") or 22050
    except Exception:  # noqa: BLE001
        pass
    return 22050


def pitch_shift(src: Path, factor: float) -> Path | None:
    """用 ffmpeg 抬高音高（不变调快慢），做出更嫩的音色。"""
    import subprocess
    import tempfile

    if abs(factor - 1.0) < 0.01:
        return src
    exe = _ffmpeg_exe()
    if not exe:
        return src
    try:
        rate = wav_rate(src.read_bytes())
        dst = Path(tempfile.gettempdir()) / f"qqds_pitch_{int(time.time() * 1000)}.wav"
        af = f"asetrate={rate}*{factor:.4f},aresample={rate},atempo={1.0 / factor:.4f}"
        subprocess.run(
            [exe, "-y", "-loglevel", "error", "-i", str(src), "-af", af,
             "-ac", "1", "-ar", str(rate), str(dst)],
            timeout=90, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if dst.exists() and dst.stat().st_size > 1000:
            try:
                src.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            return dst
        return src
    except Exception as e:  # noqa: BLE001
        log.warning("变声失败: %r", e)
        return src


def default_cn_voice() -> str:
    """挑一个中文女声。"""
    return "Microsoft Huihui Desktop"


def sapi_wav(text: str, voice: str = "", rate: int = 1, pitch: float = 1.18) -> Path | None:
    """用 Windows 自带 SAPI 合成 wav（文字走 base64 传入，避免控制台码页把中文搞乱）。"""
    import subprocess
    import tempfile

    out = Path(tempfile.gettempdir()) / f"qqds_tts_{int(time.time() * 1000)}.wav"
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    voice = voice or default_cn_voice()
    vq = voice.replace("'", "''")
    ps = (
        "$ErrorActionPreference='Stop';"
        "Add-Type -AssemblyName System.Speech;"
        "$t=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + b64 + "'));"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "try{$s.SelectVoice('" + vq + "')}catch{};"
        "$s.Rate=" + str(int(rate)) + ";"
        "$s.SetOutputToWaveFile('" + str(out).replace("'", "''") + "');"
        "$s.Speak($t);"
        "$s.Dispose();"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            timeout=90, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if not (out.exists() and out.stat().st_size > 1000):
            return None
        return pitch_shift(out, float(pitch))
    except Exception as e:  # noqa: BLE001
        log.warning("TTS 失败: %r", e)
        return None


def edge_tts_wav(text: str, voice: str = "zh-CN-XiaoyiNeural",
                 rate: str = "+0%", pitch: str = "+0Hz") -> Path | None:
    """用 Edge 的神经网络音色合成（需要联网），mp3 转 wav。"""
    import asyncio as _asyncio
    import subprocess
    import tempfile

    try:
        import edge_tts  # type: ignore
    except ImportError:
        return None

    async def _run(mp3: Path):
        comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
        with open(mp3, "wb") as f:
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])

    td = Path(tempfile.mkdtemp())
    mp3 = td / "tts.mp3"
    last = None
    for attempt in range(3):
        try:
            _asyncio.run(_run(mp3))
            if mp3.exists() and mp3.stat().st_size > 1000:
                break
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("Edge TTS 第 %s 次失败: %r", attempt + 1, e)
            time.sleep(1.2 * (attempt + 1))
    try:
        if not mp3.exists() or mp3.stat().st_size < 1000:
            if last:
                log.warning("Edge TTS 放弃: %r", last)
            return None
        exe = _ffmpeg_exe()
        if not exe:
            return None
        wav = Path(tempfile.gettempdir()) / f"qqds_edge_{int(time.time() * 1000)}.wav"
        subprocess.run(
            [exe, "-y", "-loglevel", "error", "-i", str(mp3),
             "-ac", "1", "-ar", "24000", str(wav)],
            timeout=60, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return wav if wav.exists() and wav.stat().st_size > 1000 else None
    except Exception as e:  # noqa: BLE001
        log.warning("Edge TTS 失败: %r", e)
        return None


_LOCAL_TTS_CACHE: dict[str, object] = {}
_LOCAL_TTS_FAILED: set[str] = set()
LOCAL_TTS_ROOT = BASE_DIR / "tts_model"


def _get_local_tts(model_dir: Path):
    import sherpa_onnx  # type: ignore

    key = str(model_dir)
    if key in _LOCAL_TTS_CACHE:
        return _LOCAL_TTS_CACHE[key]
    onnx = next(model_dir.glob("*.onnx"), None)
    if not onnx:
        raise FileNotFoundError(f"{model_dir} 里没有 .onnx 模型")
    dict_dir = model_dir / "dict"
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=str(onnx),
                lexicon=str(model_dir / "lexicon.txt"),
                tokens=str(model_dir / "tokens.txt"),
                dict_dir=str(dict_dir) if dict_dir.is_dir() else "",
            ),
            num_threads=2,
            provider="cpu",
        ),
        rule_fsts=",".join(
            str(model_dir / f)
            for f in ("date.fst", "number.fst", "phone.fst", "new_heteronym.fst")
            if (model_dir / f).exists()
        ),
        max_num_sentences=1,
    )
    tts = sherpa_onnx.OfflineTts(cfg)
    _LOCAL_TTS_CACHE[key] = tts
    log.info("本地 TTS 已加载: %s (%s Hz)", model_dir.name, tts.sample_rate)
    return tts


def sherpa_wav(text: str, model_name: str | None = None, speed: float = 1.0) -> Path | None:
    """本机 sherpa-onnx 中文 VITS 合成，完全离线。model_name 为 tts_model 下的子目录名。"""
    import wave

    name = model_name or "vits-piper-zh_CN-xiao_ya-medium"
    model_dir = LOCAL_TTS_ROOT / name
    if str(model_dir) in _LOCAL_TTS_FAILED:
        return None
    try:
        import numpy as np

        tts = _get_local_tts(model_dir)
        audio = tts.generate(text, sid=0, speed=float(speed))
        if not audio.samples:
            return None
        pcm = (np.array(audio.samples) * 32767).astype("<i2").tobytes()
        out = Path(tempfile.gettempdir()) / f"qqds_local_{int(time.time() * 1000)}.wav"
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(audio.sample_rate)
            w.writeframes(pcm)
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("本地 TTS 不可用 (%s): %r", name, e)
        _LOCAL_TTS_FAILED.add(str(model_dir))
        return None


def tts_wav(text: str, voice: str = "", rate: int = 1, pitch: float = 1.2,
            provider: str = "edge", edge_rate: str = "+0%",
            edge_pitch: str = "+0Hz", local_model: str = "") -> Path | None:
    """统一入口。默认链路：Edge 神经音色 → 本地模型 → SAPI，逐个兜底。"""
    order = [provider, "local", "sapi"]
    seen = set()
    for p in order:
        if not p or p in seen:
            continue
        seen.add(p)
        w = None
        if p == "edge":
            w = edge_tts_wav(text, voice or "zh-CN-XiaoyiNeural", edge_rate, edge_pitch)
        elif p in ("local", "sherpa"):
            w = sherpa_wav(text, local_model or None)
        elif p == "sapi":
            w = sapi_wav(text, "" if provider == "edge" else voice, rate, pitch)
        if w:
            log.info("语音合成: %s", p)
            return w
        log.info("语音合成失败，换下一个: %s", p)
    return None


def extract_message(message) -> tuple[str, list[str], list[str], list[dict]]:
    """拆出 (文本, 图片URL, 图片file_id, 语音/视频等媒体)。"""
    if isinstance(message, str):
        return message.strip(), [], [], []
    texts, urls, files, extra = [], [], [], []
    for seg in message or []:
        t = seg.get("type")
        d = seg.get("data") or {}
        if t == "text":
            texts.append(d.get("text", ""))
        elif t == "image":
            if d.get("url"):
                urls.append(d["url"])
            elif d.get("file"):
                files.append(d["file"])
            texts.append("[图片]")
        elif t in ("mface", "marketface"):
            texts.append("[表情]")
        elif t == "face":
            texts.append("[表情]")
        elif t == "record":
            texts.append("[语音]")
            if d.get("url") or d.get("file"):
                extra.append({"type": "record", "url": d.get("url"), "file": d.get("file")})
        elif t == "video":
            texts.append("[视频]")
            if d.get("url") or d.get("file"):
                extra.append({"type": "video", "url": d.get("url"), "file": d.get("file")})
        elif t == "file":
            texts.append("[文件]")
        elif t == "at":
            qq = d.get("qq")
            if qq != "all":
                texts.append(f"@{qq} ")
        elif t == "reply":
            continue
    return "".join(texts).strip(), urls, files, extra


def build_user_content(text: str, image_urls: list[str], detail: str) -> str | list:
    """没有图片时返回纯字符串；有图片时返回 OpenAI 多模态 block 数组。"""
    if not image_urls:
        return text
    blocks: list[dict] = [{"type": "text", "text": text or "看看这个"}]
    for u in image_urls:
        blocks.append({"type": "image_url", "image_url": {"url": u, "detail": detail}})
    return blocks


def split_bubbles(text: str, max_chars: int = 40, max_count: int = 8) -> list[str]:
    """把一段回复切成「像人连发」的若干短消息。

    优先按句末标点和换行切；切出来太短就并到上限；条数超了就把尾巴并进最后一条。
    """
    text = text.strip()
    if not text:
        return []
    # 先按标点/换行拆成句子，保留标点
    raw = re.split(r"(?<=[。！？!?～~…])|\n+", text)
    sents = [s.strip() for s in raw if s and s.strip()]
    if not sents:
        return [text]

    bubbles: list[str] = []
    cur = ""
    for s in sents:
        if not cur:
            cur = s
        elif len(cur) + len(s) <= max_chars:
            cur += s
        else:
            bubbles.append(cur)
            cur = s
    if cur:
        bubbles.append(cur)

    # 单条还是太长，硬切
    out: list[str] = []
    for b in bubbles:
        while len(b) > max_chars:
            cut = max(b.rfind("，", 0, max_chars), b.rfind("、", 0, max_chars))
            if cut < max_chars * 0.5:
                cut = max_chars
            else:
                cut += 1
            out.append(b[:cut].strip())
            b = b[cut:].strip()
        if b:
            out.append(b)

    # 条数超限：把后面几条并进最后一条
    if len(out) > max_count:
        head = out[: max_count - 1]
        tail = "".join(out[max_count - 1:])
        head.append(tail)
        out = head
    return [x for x in out if x]


def bubble_delay(text: str, cfg) -> float:
    b = cfg.raw.get("bubbles") or DEFAULT_CONFIG["bubbles"]
    per = float(b.get("delay_per_char", 0.055))
    lo = float(b.get("min_delay", 0.6))
    hi = float(b.get("max_delay", 3.2))
    return max(lo, min(hi, len(text) * per + random.uniform(0.25, 0.7)))


def split_chunks(text: str, limit: int) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks, buf = [], ""
    for para in re.split(r"(?<=\n)", text):
        while len(para) > limit:
            cut = para.rfind("。", 0, limit)
            if cut < limit * 0.5:
                cut = para.rfind("\n", 0, limit)
            if cut < limit * 0.5:
                cut = limit
            else:
                cut += 1
            piece, para = para[:cut], para[cut:]
            if buf and len(buf) + len(piece) > limit:
                chunks.append(buf)
                buf = ""
            buf += piece
            if len(buf) >= limit:
                chunks.append(buf)
                buf = ""
        if buf and len(buf) + len(para) > limit:
            chunks.append(buf)
            buf = ""
        buf += para
        if len(buf) >= limit:
            chunks.append(buf)
            buf = ""
    if buf.strip():
        chunks.append(buf.strip())
    return [c.strip() for c in chunks if c.strip()]


def insert_breaks(text: str, max_lines: int = 3) -> str:
    """单行长文本隔几句插一个换行，QQ 上更好读。"""
    if "\n" in text:
        return text
    if len(text) < 90:
        return text
    out, cnt = [], 0
    for ch in text:
        out.append(ch)
        if ch in "。！？!?":
            cnt += 1
            if cnt >= max_lines:
                out.append("\n")
                cnt = 0
    return "".join(out).strip()


# ----------------------------------------------------------------------------
# 桥接主体
# ----------------------------------------------------------------------------
def _owner_key(cfg=None):
    """主账号的存储键：从配置的 owner_qq 推导，避免把 QQ 号写死在代码里。"""
    qq = ""
    try:
        qq = str((cfg or {}).get("owner_qq") or "")
    except Exception:
        pass
    if not qq:
        try:
            import json as _json
            from pathlib import Path as _Path
            _conf = _json.loads((_Path(__file__).resolve().parent / "config.json")
                                .read_text(encoding="utf-8"))
            qq = str(_conf.get("owner_qq") or "")
        except Exception:
            qq = ""
    return ("u" + qq) if qq else "u_unknown"


class Bridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api_key = resolve_api_key(cfg)
        self.store = Store(cfg.history_ttl_hours)
        self.pending: dict[str, asyncio.Future] = {}
        self.echo_seq = 0
        self.ws = None
        self.owners: set[int] = cfg.owners | set(load_bound_owner())
        self.last_reply_at: dict[int, float] = {}
        # 主动消息状态
        self.last_active_user: int | None = None
        self.last_user_msg_ts: float = 0.0
        self.group_hits: dict[int, list[float]] = {}
        self.pro_state: dict = self._load_pro_state()
        self.mem = Memory(cfg)
        self.persona = Persona(cfg)
        self.todo = Pending()
        self.send_fail_streak = 0

    # -- 向量记忆：按语义捞旧事 -------------------------------------------
    SELF_EDIT_RE = re.compile(r"\[\[自改\]\](.*?)\[\[/自改\]\]", re.S)
    PATCH_RE = re.compile(r"\[\[提议\]\](.*?)\[\[/提议\]\]", re.S)

    def _strip_blocks(self, reply: str) -> str:
        """把给程序看的块从她说的话里去掉，只留人话。"""
        out = self.SELF_EDIT_RE.sub("", reply or "")
        out = self.PATCH_RE.sub("", out)
        return out.strip()

    def _consume_blocks(self, reply: str, key: str) -> None:
        """处理她自己改人设的请求，以及她提议改代码的请求。"""
        if not reply:
            return
        # ① 改自己的人设（白名单，直接生效，有日志）
        for m in self.SELF_EDIT_RE.finditer(reply):
            try:
                edit = json.loads(m.group(1).strip())
                changed = self.persona.apply_self_edit(edit)
                if changed:
                    log.info("[自改] %s 改了 %s", key, changed)
            except Exception:  # noqa: BLE001
                log.debug("自改解析失败", exc_info=True)
        # ② 提议改代码（不生效，只落盘等裁决）
        for m in self.PATCH_RE.finditer(reply):
            try:
                prop = json.loads(m.group(1).strip())
                self._save_proposal(prop, key)
            except Exception:  # noqa: BLE001
                log.debug("提议解析失败", exc_info=True)

    def _save_proposal(self, prop: dict, key: str) -> None:
        """她提议改代码：只记录，绝不自动应用。"""
        prop_dir = DATA_DIR / "proposals"
        prop_dir.mkdir(parents=True, exist_ok=True)
        pid = time.strftime("%Y%m%d_%H%M%S")
        rec = {"id": pid, "ts": time.time(), "key": key,
               "file": str(prop.get("file") or "")[:80],
               "reason": str(prop.get("reason") or "")[:400],
               "diff": str(prop.get("diff") or "")[:4000],
               "status": "待裁决"}
        (prop_dir / f"{pid}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("[提议] 她提交了一条改动建议 %s → %s", pid, rec["file"])

    @staticmethod
    def skip_recall(q: str) -> bool:
        """问实时信息时不翻旧记忆（该去查，不该"想起"）。"""
        import re as _re
        pat = r"(几号|几点|星期几|天气|气温|多少钱|汇率|股价|比分|实时|现在.*多少)"
        return bool(_re.search(pat, q or ""))

    def vec_block(self, text: str, exclude: set[str] | None = None) -> str:
        """按当前这句话去向量库里捞相关的旧记录。失败一律返回空字符串。"""
        if _vecmem is None:
            return ""
        cfg = self.cfg.raw.get("vecmem") or DEFAULT_CONFIG.get("vecmem") or {}
        if not cfg.get("enable", True):
            return ""
        q = (text or "").strip()
        if len(q) < int(cfg.get("min_query_len", 4)):
            return ""
        if self.skip_recall(q):
            return ""
        try:
            idx = _vecmem.get_index(self.cfg)
            if idx is None:
                return ""
            # 第一层：SQL 圈定候选范围（保底：最近 N 天）
            allowed = None
            prefer = None
            if _memdb is not None:
                try:
                    if _memdb.available():
                        allowed = _memdb.ids_within_days(int(cfg.get("max_age_days", 365)))
                        prefer = _memdb.ids_containing(q)
                except Exception:  # noqa: BLE001
                    allowed, prefer = None, None
            # 第二层：在候选里按语义排序
            hits = idx.search(
                q,
                k=int(cfg.get("topk", 2)),
                threshold=float(cfg.get("threshold", 0.58)),
                exclude_ids=exclude or set(),
                allowed_ids=allowed,
                prefer_ids=prefer,
                prefer_bonus=float(cfg.get("prefer_bonus", 0.03)),
            )
            self._last_hits = hits
            hits = [h for h in hits if len(h.get("text") or "") >= int(cfg.get("min_chunk_len", 15))]
            if not hits:
                return ""
            return _vecmem.format_block(hits, int(cfg.get("max_chars", 150)))
        except Exception:  # noqa: BLE001
            log.debug("向量检索失败", exc_info=True)
            return ""

    def vec_sync(self):
        """把新增的日记/对话增量入库（失败不影响对话）。"""
        if _vecmem is None:
            return
        try:
            idx = _vecmem.get_index(self.cfg)
            if idx is None:
                return
            n = idx.sync()
            if n:
                log.info("[向量记忆] 新增 %s 条", n)
            if _memdb is not None:
                try:
                    _memdb.sync()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            log.debug("向量同步失败", exc_info=True)

    # -- 系统提示词 ---------------------------------------------------------
    def system_text(self) -> str:
        base = self.cfg.system_prompt or ""
        style = self.cfg.style_prompt or ""
        soul = ""
        try:
            soul = self.persona.soul_block()
        except Exception:  # noqa: BLE001
            soul = ""
        pinned = ""
        try:
            if _pins is not None:
                pinned = _pins.block()
        except Exception:  # noqa: BLE001
            pinned = ""
        diary = ""
        try:
            diary = self.persona.diary_block()
        except Exception:  # noqa: BLE001
            diary = ""
        shared = ""
        try:
            shared = self.persona.shared_block()
        except Exception:  # noqa: BLE001
            shared = ""
        letters = ""
        try:
            letters = self.persona.letters_block()
        except Exception:  # noqa: BLE001
            letters = ""
        rel = ""
        try:
            rel = "\n\n" + self.persona.relationship_line()
        except Exception:  # noqa: BLE001
            rel = ""
        vn = ""
        try:
            vn = getattr(self, "_verify_note", "") or ""
        except Exception:  # noqa: BLE001
            vn = ""
        rules = """
\n\n【引用旧事时的三条硬规矩】
1. 提到具体事件（哪天发生了什么），必须能说清日期；日期不确定就说「我记不清是哪天」，不要用「今天／昨天／上次」去猜。
2. 你自己说过的话、提过的建议，不等于他的事实。看到「我（小鲨）：」开头的记录，那是你说的，不是他说的。
3. 记不清就说不清。编一个具体日期或细节，比说「忘了」伤害大得多。
"""
        return base + soul + pinned + rel + letters + shared + diary + rules + style + (("\n\n" + vn) if vn else "")

    # -- 主动消息状态 -------------------------------------------------------
    def _load_pro_state(self) -> dict:
        if PROACTIVE_PATH.exists():
            try:
                return json.loads(PROACTIVE_PATH.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
        return {}

    def _save_pro_state(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            PROACTIVE_PATH.write_text(
                json.dumps(self.pro_state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            pass

    # -- OneBot 调用 ---------------------------------------------------------
    async def call(self, action: str, params: dict, timeout: float = 30):
        if self.ws is None:
            raise RuntimeError("WebSocket 未连接")
        self.echo_seq += 1
        echo = f"e{self.echo_seq}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[echo] = fut
        await self.ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self.pending.pop(echo, None)
        # QQ 底层「网络连接异常」时，会话仍显示在线但收发全废 —— 连续失败就让守护重启 NapCat
        try:
            msg = str((resp or {}).get("message") or "")
            bad = ("1006514" in msg) or ("网络连接异常" in msg)
            if bad and str(action).startswith(("send_", "set_")):
                self.send_fail_streak += 1
                log.warning("发送失败（%s/%s）：网络连接异常", self.send_fail_streak, 3)
                if self.send_fail_streak >= 3:
                    RESTART_FLAG.write_text(str(time.time()), encoding="utf-8")
                    self.send_fail_streak = 0
                    log.error("连续 3 次发送失败，已请求重启 NapCat 恢复连接")
            elif bad is False:
                self.send_fail_streak = 0
        except Exception:  # noqa: BLE001
            pass
        return resp

    async def send_text(self, user_id: int, text: str, group_id: int | None = None):
        if not text.strip():
            return
        seg = [{"type": "text", "data": {"text": text}}]
        if group_id:
            await self.call("send_group_msg", {"group_id": group_id, "message": seg})
        else:
            await self.call("send_private_msg", {"user_id": user_id, "message": seg})

    async def send_bubbles(self, user_id: int, text: str, group_id: int | None = None,
                           first_delay: float | None = None):
        """把回复拆成多条短消息，按「打字时间」逐条发出。"""
        b = self.cfg.raw.get("bubbles") or DEFAULT_CONFIG["bubbles"]
        if not b.get("enable", True):
            for chunk in split_chunks(text, int(self.cfg.max_chunk_chars)):
                await self.send_text(user_id, chunk, group_id)
            return
        parts = split_bubbles(
            text,
            max_chars=int(b.get("max_chars", 40)),
            max_count=int(b.get("max_count", 8)),
        )
        for i, part in enumerate(parts):
            if i == 0:
                d = first_delay if first_delay is not None else (
                    self.cfg.initial_wait_sec + random.random() * 0.5)
                if d > 0:
                    await asyncio.sleep(d)
            else:
                await asyncio.sleep(bubble_delay(part, self.cfg))
            await self.send_text(user_id, part, group_id)

    # -- 表情 / 表情包 / 语音 ------------------------------------------------
    async def send_face(self, user_id: int, face_id: int, group_id: int | None = None):
        seg = [{"type": "face", "data": {"id": str(face_id)}}]
        if group_id:
            await self.call("send_group_msg", {"group_id": group_id, "message": seg})
        else:
            await self.call("send_private_msg", {"user_id": user_id, "message": seg})

    def _pick_sticker(self) -> Path | None:
        st = self.cfg.raw.get("stickers") or DEFAULT_CONFIG["stickers"]
        if not st.get("enable"):
            return None
        d = BASE_DIR / str(st.get("dir") or "stickers")
        if not d.is_dir():
            return None
        files = [p for p in d.iterdir()
                 if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")]
        return random.choice(files) if files else None

    async def send_sticker(self, user_id: int, group_id: int | None = None) -> bool:
        p = self._pick_sticker()
        if not p:
            return False
        try:
            b64 = base64.b64encode(p.read_bytes()).decode("ascii")
            seg = [{"type": "image", "data": {"file": "base64://" + b64}}]
            if group_id:
                await self.call("send_group_msg", {"group_id": group_id, "message": seg}, timeout=60)
            else:
                await self.call("send_private_msg", {"user_id": user_id, "message": seg}, timeout=60)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("表情包发送失败: %r", e)
            return False

    async def send_record(self, user_id: int, wav: Path, group_id: int | None = None) -> bool:
        try:
            b64 = base64.b64encode(wav.read_bytes()).decode("ascii")
            seg = [{"type": "record", "data": {"file": "base64://" + b64}}]
            r = await self.call("send_private_msg" if not group_id else "send_group_msg",
                                {"user_id": user_id, "message": seg} if not group_id
                                else {"group_id": group_id, "message": seg}, timeout=90)
            ok = (r or {}).get("status") == "ok" or (r or {}).get("retcode") == 0
            if not ok:
                log.warning("语音发送返回异常: %s", str(r)[:200])
            return ok
        except Exception as e:  # noqa: BLE001
            log.warning("语音发送失败: %r", e)
            return False

    # -- 事件处理 -----------------------------------------------------------
    async def on_event(self, evt: dict):
        post_type = evt.get("post_type")

        if post_type is None and "echo" in evt:
            fut = self.pending.get(evt["echo"])
            if fut and not fut.done():
                fut.set_result(evt)
            return

        if post_type != "message":
            return
        if evt.get("message_type") == "group":
            await self.on_group(evt)
        elif evt.get("message_type") == "private":
            await self.on_private(evt)

    async def on_private(self, evt: dict):
        user_id = int(evt.get("user_id", 0))
        if user_id == (evt.get("self_id") or -1):
            return
        text, urls, files, extra = extract_message(evt.get("message"))
        if not text and not urls and not files:
            return

        if user_id not in self.owners:
            if self.cfg.auto_bind_first_sender and not self.owners:
                self.owners.add(user_id)
                save_bound_owner(user_id)
                log.info("首次私聊，已绑定 owner = %s", user_id)
                await self.send_text(user_id, "已绑定为我的主人，之后只有你能使唤我。")
            else:
                log.info("忽略非 owner 私聊: %s -> %r", user_id, text[:60])
                return

        log.info("[私聊 %s] %s%s", user_id, text[:100], f" (+{len(urls) + len(files)} 图)" if (urls or files) else "")
        self.last_active_user = user_id
        self.last_user_msg_ts = time.time()
        await self.handle(text, user_id=user_id, image_urls=urls, image_files=files, extra=extra)

    async def on_group(self, evt: dict):
        g = self.cfg.raw.get("group") or DEFAULT_CONFIG["group"]
        if not g.get("enable"):
            return
        group_id = int(evt.get("group_id", 0))
        allowed = g.get("allowed_groups") or []
        if allowed and group_id not in {int(x) for x in allowed}:
            return

        self_id = str(evt.get("self_id") or "")
        sender = evt.get("sender") or {}
        nickname = sender.get("card") or sender.get("nickname") or ""
        user_id = int(evt.get("user_id", 0))
        if user_id == (evt.get("self_id") or -1):
            return

        text, urls, files, extra = extract_message(evt.get("message"))

        # 只在「明确 @ 她本人」时说话；@全体成员 一律不理
        at_qqs = [
            str((seg.get("data") or {}).get("qq"))
            for seg in (evt.get("message") or [])
            if seg.get("type") == "at"
        ]
        at_all = "all" in at_qqs
        at_me = self_id in at_qqs
        if g.get("require_at", True) and not at_me:
            if at_all:
                log.debug("群 %s: @全体成员，不响应", group_id)
            return
        if at_all and not at_me:
            return
        text = text.replace(f"@{self_id}", "").strip()

        # 谁能触发
        if g.get("only_owner", True) and user_id not in self.owners:
            log.info("群 %s 忽略非 owner: %s(%s)", group_id, nickname, user_id)
            return

        if not text and not urls and not files:
            return

        # 群内限流
        per_min = int(g.get("rate_limit_per_min", 6))
        now = time.time()
        hits = [t for t in self.group_hits.get(group_id, []) if now - t < 60]
        if len(hits) >= per_min:
            log.info("群 %s 触发限流，忽略", group_id)
            return
        hits.append(now)
        self.group_hits[group_id] = hits

        log.info("[群 %s] %s(%s): %s", group_id, nickname, user_id, text[:80])
        await self.handle(
            text, user_id=user_id, group_id=group_id,
            image_urls=urls, image_files=files,
            conv_key=f"g{group_id}",
            speaker=f"{nickname}({user_id})",
            group_name=evt.get("group_name") or "",
            extra=extra,
        )

    # -- 主流程 -------------------------------------------------------------
    async def collect_images(self, urls: list[str], files: list[str]) -> tuple[list[str], int]:
        """把图片都变成 data:URL，返回 (成功列表, 失败数)。"""
        if not self.cfg.vision:
            return [], len(urls) + len(files)
        limit = max(1, int(self.cfg.max_images_per_msg))
        all_urls = list(urls)[:limit]
        # 有些上报只给 file id，需要问 NapCat 要下载地址
        for fid in files[: max(0, limit - len(all_urls))]:
            try:
                r = await self.call("get_image", {"file": fid})
                u = ((r or {}).get("data") or {}).get("url")
                if u:
                    all_urls.append(u)
            except Exception as e:  # noqa: BLE001
                log.warning("get_image 失败 %s: %r", fid, e)
        data_urls, failed = [], 0
        for u in all_urls:
            d = await asyncio.to_thread(download_image_data_url, u)
            if d:
                data_urls.append(d)
            else:
                failed += 1
        return data_urls, failed + max(0, len(urls) + len(files) - limit)

    async def handle(
        self, text: str, user_id: int, group_id: int | None = None,
        image_urls: list[str] | None = None, image_files: list[str] | None = None,
        conv_key: str | None = None, speaker: str | None = None,
        group_name: str = "", extra: list[dict] | None = None,
    ):
        key = conv_key or f"u{user_id}"
        cmd = text.strip().lower()
        if cmd in {"/reset", "重置", "清空", "/清空", "重新开始"}:
            self.store.reset(key)
            await self.send_text(user_id, "记忆已清空，我们重新开始。", group_id)
            return
        if cmd in {"/ping", "ping"}:
            await self.send_text(user_id, "在。", group_id)
            return
        if cmd in {"/cost", "花费", "花了多少"}:
            cnt, cost, toks = usage_summary(24)
            await self.send_text(
                user_id,
                f"最近 24 小时：{cnt} 次对话，约 {toks} tokens，花费约 ¥{cost:.4f}",
                group_id,
            )
            return
        if cmd in {"/help", "帮助", "help"}:
            await self.send_text(
                user_id,
                "直接说话就行，图片、语音、视频都能发。\n"
                "/记住 xxx 让我记住一件事，/记忆 看我记了什么，"
                "/忘记 清掉长期记忆，/reset 清临时对话，/cost 看花费。",
                group_id,
            )
            return

        # 记忆相关命令
        raw_text = text.strip()
        if raw_text.startswith(("/记住", "记住", "/记 ")) and len(raw_text) > 2:
            fact = re.sub(r"^/?(记住|记)(了)?[\s:：,，、。]*", "", raw_text).strip()
            if fact:
                self.mem.add_fact(key, fact)
                await self.send_text(user_id, "记下了。", group_id)
                return
        if cmd in {"/记忆", "记忆", "/memory"}:
            r = self.mem.rec(key)
            facts = "\n".join("- " + x for x in r.get("facts", [])) or "（空）"
            summ = r.get("summary") or "（还没形成长期印象）"
            await self.send_text(
                user_id, f"我记住的事：\n{facts}\n\n长期印象：\n{summ}"[:1200], group_id
            )
            return
        if cmd in {"/忘记", "忘记", "忘掉", "/forget"}:
            self.mem.clear(key)
            n_arc = self.mem.nuke(key)
            self.store.reset(key)
            rebuilt = ""
            try:
                if _vecmem is not None:
                    idx = _vecmem.VecIndex()
                    rebuilt = f"，索引重建 {idx.rebuild()} 条"
                    _vecmem._INDEX = idx
                if _memdb is not None:
                    _memdb.rebuild()
            except Exception as e:  # noqa: BLE001
                rebuilt = f"（索引重建失败：{str(e)[:40]}）"
            await self.send_text(
                user_id,
                f"清干净了：长期记忆、对话历史、归档原文 {n_arc} 条{rebuilt}。\n"
                "日记和信件属于你自己写的、他写的，不在此列，要删跟我说。",
                group_id,
            )
            return

        # 向量记忆相关命令
        if _vecmem is not None and (cmd.startswith(("/想起", "想起")) or cmd in {"/索引状态", "/vec", "索引状态", "/重建索引", "重建索引"}):
            if cmd.startswith(("/想起", "想起")):
                arg = raw_text.replace("/想起", "").replace("想起", "").strip()
                if not arg:
                    await self.send_text(user_id, "用法：/想起 关键词", group_id)
                    return
                # 手动查询放宽：他是主动问的，宁可多给几条
                _idx = _vecmem.get_index(self.cfg)
                hits = _idx.search(arg, k=3, threshold=0.40) if _idx else []
                if hits:
                    await self.send_text(user_id, _vecmem.format_block(hits, 150).strip()[:900], group_id)
                else:
                    await self.send_text(user_id, f"关于「{arg}」，我翻不到相关的旧事。", group_id)
                return
            if cmd in {"/索引状态", "/vec", "索引状态"}:
                idx = _vecmem.get_index(self.cfg)
                st = idx.status() if idx else {"count": 0}
                await self.send_text(user_id, f"向量记忆：{st}", group_id)
                return
            await self.send_text(user_id, "开始重建索引，稍等。", group_id)
            try:
                idx = _vecmem.VecIndex()
                n = idx.rebuild()
                _vecmem._INDEX = idx
                await self.send_text(user_id, f"重建完成：{n} 条。", group_id)
            except Exception as e:  # noqa: BLE001
                await self.send_text(user_id, f"重建失败：{str(e)[:80]}", group_id)
            return

        # 结构化精确查询（SQLite 层）
        if _memdb is not None and cmd.startswith(("/查", "查")):
            arg = raw_text.replace("/查", "").replace("查", "").strip()
            if not arg:
                await self.send_text(user_id, "用法：/查 关键词　或　/查 日记　或　/查 10-21", group_id)
                return
            try:
                type_map = {"日记": "diary", "对话": "chat", "信": "dad_letter",
                            "回信": "her_letter", "他说": "told"}
                rows = []
                if arg in type_map:
                    rows = _memdb.query(type_=type_map[arg], limit=8)
                elif len(arg) == 5 and arg[2] == "-":
                    rows = _memdb.query(since="2026-" + arg, until="2026-" + arg, limit=8)
                else:
                    rows = _memdb.query(contains=arg, limit=8)
                if not rows:
                    await self.send_text(user_id, f"库里没有跟「{arg}」匹配的记录。", group_id)
                    return
                lines = [f"- [{r['date']} {r['type']}] {(r['text'] or '')[:60]}" for r in rows]
                await self.send_text(user_id, ("查到 " + str(len(rows)) + " 条：\n" + "\n".join(lines))[:900], group_id)
            except Exception as e:  # noqa: BLE001
                await self.send_text(user_id, f"查询出错：{str(e)[:80]}", group_id)
            return

        if cmd in {"/为什么", "为什么"}:
            try:
                lines = DECISION_LOG.read_text(encoding="utf-8").splitlines() if DECISION_LOG.exists() else []
                if not lines:
                    await self.send_text(user_id, "还没有决策记录。", group_id)
                    return
                d = json.loads(lines[-1])
                hits = d.get("recalled") or []
                hit_txt = "\n".join(
                    f"  - {h.get('score')} [{h.get('date')} {h.get('type')}] {h.get('text')}" for h in hits
                ) or "  （没有召回旧事）"
                await self.send_text(
                    user_id,
                    ("上一轮：" + str(d.get("user"))[:60]
                     + "\n心情：" + str(d.get("mood"))
                     + "\n召回的旧事：\n" + hit_txt
                     + "\n注入的块：" + "、".join(d.get("injected") or []) or "无")[:900],
                    group_id,
                )
            except Exception as e:  # noqa: BLE001
                await self.send_text(user_id, f"读记录失败：{str(e)[:60]}", group_id)
            return

        if _pins is not None and cmd.startswith(("/钉住", "钉住")):
            arg = raw_text.replace("/钉住", "").replace("钉住", "").strip()
            if not arg:
                await self.send_text(user_id, "用法：/钉住 那句话", group_id)
                return
            pin = _pins.add(arg, reason="他手动钉的", source="manual")
            await self.send_text(user_id, "钉住了，永远不会被压缩。" if pin else "这句已经在钉子上了。", group_id)
            return
        if _pins is not None and cmd in {"/钉子", "钉子"}:
            st = _pins.stats()
            await self.send_text(user_id, f"钉子共 {st['count']} 条（自动 {st['auto']} / 手动 {st['manual']}）。", group_id)
            return
        if _pins is not None and cmd.startswith(("/拔钉", "拔钉")):
            arg = raw_text.replace("/拔钉", "").replace("拔钉", "").strip()
            if not arg:
                await self.send_text(user_id, "用法：/拔钉 关键词", group_id)
                return
            n = _pins.remove(arg)
            await self.send_text(user_id, f"拔掉 {n} 条。" if n else "没找到匹配的钉子。", group_id)
            return

        if cmd in {"/心情", "心情"}:
            d = self.persona.d
            await self.send_text(
                user_id,
                f"心情：{d.get('mood', '平淡')}　精力：{d.get('energy', 60)}/100",
                group_id,
            )
            return
        if cmd in {"/日记", "日记"}:
            last = self.persona.last_diary()
            if last:
                await self.send_text(user_id, f"{last.get('date')} 的日记：\n{last.get('text')}", group_id)
            else:
                await self.send_text(user_id, "今天还没写呢。", group_id)
            return
        if cmd in {"/发说说", "发说说", "/说说"}:
            await self._write_diary()
            await self.send_text(user_id, "写好了，去空间看看。", group_id)
            return

        # 翻某一天的日记（把那天的原文调进这一轮上下文）
        letter_extra = ""
        if cmd.startswith(("/翻日记", "翻日记", "/日记 ")):
            arg = raw_text.replace("/翻日记", "").replace("翻日记", "").strip()
            ds = self.persona.d.get("diary") or []
            today = now_bj().date()
            alias = {"今天": today, "昨天": today - dt.timedelta(days=1),
                     "前天": today - dt.timedelta(days=2)}
            hits = []
            if arg in alias:
                target = alias[arg].isoformat()
                hits = [x for x in ds if str(x.get("date") or "").startswith(target)]
            elif arg:
                hits = [x for x in ds if arg in str(x.get("date") or "")]
            else:
                hits = ds[-3:]
            if not hits:
                await self.send_text(user_id, f"没找到「{arg or '最近'}」那天的日记。", group_id)
                return
            body = "\n\n".join(f"【{x.get('date')}】\n{(x.get('text') or '').strip()}" for x in hits)
            letter_extra = ("\n\n【你要翻的日记原文】（这是你自己写的，不是别人说的）\n" + body)
            text = f"（他把 {arg or '最近几天'} 的日记翻给你看了，你自己再读一遍）"
            log.info("翻日记: %s，%s 篇", arg or "最近", len(hits))

        if cmd in {"/读信", "读信", "/重读", "重读"}:
            fp = Path(str(self.persona.d.get("dad_letter_file") or ""))
            if fp.is_file():
                try:
                    body = fp.read_text(encoding="utf-8")
                    letter_extra = "\n\n【爸爸写给你的信·全文】（慢慢读，别急着回）\n" + body
                    text = "（他把那封信又发给你了，你重新读了一遍）"
                    log.info("重读爸爸的信，%s 字", len(body))
                except Exception as e:  # noqa: BLE001
                    log.warning("读信失败: %r", e)
            if not letter_extra:
                await self.send_text(user_id, "信没找到。", group_id)
                return

        decision = {"ts": time.time(), "key": key, "user": (text or "")[:120],
                    "mood": self.persona.d.get("mood"),
                    "traits": dict(self.persona.d.get("traits") or {}),
                    "recalled": [], "injected": []}

        self.persona.touch(text, replied=True)
        # 关系变量：他来了 → 失望慢慢消退、信任慢慢涨
        try:
            self.persona.d["last_seen"] = time.time()
            dis = float(self.persona.d.get("disappoint") or 0)
            tr = float(self.persona.d.get("trust") or 55)
            if dis > 0:
                self.persona.d["disappoint"] = max(0.0, dis - 1.5)
            if tr < 100:
                self.persona.d["trust"] = min(100.0, tr + 0.3)
            self.persona.save()
        except Exception:  # noqa: BLE001
            log.debug("关系变量更新失败", exc_info=True)

        # 自动识别"该记住的重话"并钉住（纯规则，0 成本）
        try:
            if _pins is not None and text and not group_id:
                _pin = _pins.auto_pin(text)
                if _pin:
                    log.info("[钉住] %s", str(_pin.get("text"))[:60])
        except Exception:  # noqa: BLE001
            log.debug("自动钉住失败", exc_info=True)
        try:
            self.todo.expire(key)
            self.todo.scan(key, text)
        except Exception:  # noqa: BLE001
            log.debug("待办扫描失败", exc_info=True)

        # 正在忙：大概率只回一句短的，而且不调模型（省钱也像人）
        # 连续打扰超过 3 句：先不忙了，过来陪他
        if not self.persona.is_busy() and not group_id:
            if self.persona.d.get("busy_msg_count"):
                self.persona.d["busy_msg_count"] = 0
        if self.persona.is_busy() and not group_id:
            count = int(self.persona.d.get("busy_msg_count") or 0) + 1
            self.persona.d["busy_msg_count"] = count
            if count > 3:
                plan = self.persona.d.get("busy_plan") or {}
                remain = 0.0
                if plan.get("date") == now_bj().date().isoformat():
                    remain = max(0.0, (float(plan["end"]) - time.time()) / 60)
                self.persona.d["busy_until"] = 0
                self.persona.d["busy_msg_count"] = 0
                self.persona.d["busy_resume_remain"] = round(remain, 1)
                self.persona.save()
                log.info("[忙] 连发 %s 句，先不忙了陪你；剩 %s 分钟等聊完再顺延", count - 1, round(remain))
            elif random.random() < 0.9:
                busy_lines = [
                    "在忙，等会儿找你", "先不聊了，我这儿有点事", "等一下，忙完回你",
                    "emmm 在弄东西，晚点说", "乖，等我一会儿", "我这会儿腾不出手",
                    "手上正忙着，五分钟", "等下啊，弄完这点就来", "别催，快了",
                    "嗯…等我一下下", "现在不行，一会儿", "忙着呢，等会说",
                    "我先记着你这句话，回头答", "手上停不下来，马上", "就快好了，别走",
                    "稍等，我这边收个尾", "在弄事，马上就好", "我这儿还没完事，你先待会儿",
                ]
                await asyncio.sleep(random.uniform(1.5, 4.0))
                await self.send_text(user_id, random.choice(busy_lines), group_id)
                self.last_reply_at[user_id] = time.time()
                log.info("[忙] 短回复，跳过模型（连发 %s 句）", count)
                return

        now = time.time()
        gap = now - self.last_reply_at.get(user_id, 0)
        if gap < self.cfg.min_reply_interval_sec:
            await asyncio.sleep(self.cfg.min_reply_interval_sec - gap)

        # 语音 / 视频处理
        extra = extra or []
        media_notes: list[str] = []
        video_frames: list[str] = []
        for m in extra:
            url = m.get("url")
            if not url and m.get("file"):
                try:
                    r = await self.call("get_record" if m["type"] == "record" else "get_file",
                                        {"file": m["file"]})
                    url = ((r or {}).get("data") or {}).get("url")
                except Exception:  # noqa: BLE001
                    url = None
            if not url:
                media_notes.append("（有个语音/视频我没取到）")
                continue
            data = await asyncio.to_thread(download_bytes, url)
            if not data:
                media_notes.append("（有个语音/视频下载失败）")
                continue
            if m["type"] == "record":
                txt = await asyncio.to_thread(transcribe_audio, data, ".amr")
                if txt:
                    media_notes.append(f"（语音转文字：{txt}）")
                else:
                    media_notes.append("（用户发了一条语音，我听不了内容）")
            else:
                frame = await asyncio.to_thread(video_first_frame, data)
                if frame:
                    video_frames.append("data:image/jpeg;base64," + base64.b64encode(frame).decode())
                    media_notes.append("（用户发了一段视频，下面这帧是视频画面）")
                else:
                    media_notes.append("（用户发了一段视频，我只取到文字，看不到画面）")

        # 图片处理
        data_urls: list[str] = []
        note = ""
        if image_urls or image_files:
            data_urls, failed = await self.collect_images(image_urls or [], image_files or [])
            if failed and not data_urls:
                note = "（你发的图片我没取到，可能已过期）"
            elif failed:
                note = f"（有 {failed} 张图没取到）"
        if note and not data_urls:
            await self.send_text(user_id, note.strip("（）") , group_id)
            # 仍然把文字交给模型

        # 手机屏幕（只对私聊，群里不抓）
        screen_note = ""
        sc = self.cfg.raw.get("screen") or DEFAULT_CONFIG["screen"]
        if sc.get("enable") and sc.get("attach_on_message", True) and not group_id:
            shot, changed = await asyncio.to_thread(screen_data_url, self.cfg, False)
            if shot:
                data_urls.append(shot)
                screen_note = "\n（最后一张图是他手机当前屏幕，可以自然地带一句，别像念菜单）"
            elif not changed:
                pass

        history = self.store.get(key)
        ask = text or ("（发了图片）" if data_urls else "（发了消息）")
        ask = stamp_bj() + self.persona.mood_hint() + " " + ask
        if media_notes:
            ask += "\n" + "".join(media_notes)
        ask += screen_note
        if speaker:
            ask = f"{speaker}: {ask}"
        user_block = build_user_content(ask, data_urls + video_frames, self.cfg.image_detail)
        sys_text = self.system_text()
        if group_id:
            sys_text += (
                f"\n\n【场景】你在一个 QQ 群里（{group_name or group_id}），别人 @ 了你。"
                "群里说话更短更随意，一两句就好，别刷屏，别用群发腔调。"
            )
        mem_block = self.mem.block(key, text)
        if mem_block:
            sys_text += mem_block
        try:
            ph = self.todo.hint(key)
            if ph:
                sys_text += ph
        except Exception:  # noqa: BLE001
            pass
        if letter_extra:
            sys_text += letter_extra

        # 按语义捞旧事（她自己"想起来"，不需要他发命令）
        try:
            recent_txt = "\n".join(str(x.get("content") or "") for x in history[-6:])
            excl = set()
            if _vecmem is not None:
                for h in _vecmem.all_chunks():
                    if h["text"][:40] and h["text"][:40] in recent_txt:
                        excl.add(h["id"])
            vb = self.vec_block(text, excl)
            if vb:
                sys_text += vb
                log.info("[向量记忆] 命中旧事")
        except Exception:  # noqa: BLE001
            log.debug("向量块组装失败", exc_info=True)
        sys_text += (
            "\n\n【时间】用户消息开头的括号是当前北京时间，用来判断今天几号、早晚。"
            "直接用，不要复述这个时间戳。"
            "\n【联网】遇到下面这些情况，**必须先调用 web_search 再回答**，不要凭记忆猜："
            "新闻时事、天气、价格行情、比分赛程、股票、软件/游戏/手机版本、"
            "你不熟悉的人名地名作品名事件、任何可能已经变化的事实、"
            "以及用户说了「查一下 / 搜一下 / 最新 / 现在多少钱」之类的话。"
            "搜完基于结果回答，结果矛盾就说明矛盾，搜不到就直说搜不到——"
            "但绝对不要用旧记忆冒充最新信息。"
            "只有纯闲聊、情绪安慰、你们之间的事、稳定的常识，才不搜。"
        )
        messages = [{"role": "system", "content": sys_text}] + history + [
            {"role": "user", "content": user_block}
        ]

        try:
            reply, usage, searches = await self._generate(messages)
        except Exception as e:  # noqa: BLE001
            log.exception("生成失败")
            await self.send_text(user_id, f"我这边出了点问题，稍后再试（{str(e)[:80]}）", group_id)
            return

        # 决策日志：这一轮用了什么、为什么（不是让模型事后编解释）
        try:
            decision["recalled"] = [
                {"id": h.get("id"), "score": h.get("score"), "type": h.get("type"),
                 "date": h.get("date"), "text": (h.get("text") or "")[:60]}
                for h in (getattr(self, "_last_hits", None) or [])
            ]
            _L = locals()
            decision["injected"] = [
                name for name, v in (
                    ("记忆块", _L.get("mem_block")),
                    ("未完成的事", _L.get("ph")),
                    ("向量召回", _L.get("vb")),
                    ("信/翻日记", _L.get("letter_extra")),
                ) if v
            ]
            decision["reply_len"] = len(reply or "")
            with DECISION_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(decision, ensure_ascii=False) + "\n")
            self._last_hits = []
        except Exception:  # noqa: BLE001
            log.debug("决策日志写入失败", exc_info=True)

        if searches:
            ws = self.cfg.raw.get("web_search") or DEFAULT_CONFIG["web_search"]
            scost = searches * float(ws.get("price_per_call", 0.005))
            log_usage({}, scost, is_peak_now(), user_id=user_id, kind="search")
            log.info("联网搜索 %s 次，≈¥%.3f", searches, scost)

        if usage:
            peak = is_peak_now()
            cost = calc_cost(self.cfg, usage, peak)
            log_usage(usage, cost, peak, user_id=user_id, kind='chat')
            log.info(
                "用量 in=%s(cache %s/%s) out=%s 花费≈¥%.5f%s",
                usage.get("prompt_tokens"),
                usage.get("prompt_cache_hit_tokens"),
                usage.get("prompt_cache_miss_tokens"),
                usage.get("completion_tokens"),
                cost,
                " [高峰]" if peak else "",
            )

        # 实时核验：她话里提到的具体事件，记录里有没有
        try:
            vcfg = self.cfg.raw.get("verify_claims") or {}
            if vcfg.get("enable", True) and reply:
                import claim_audit as _ca
                res = _ca.verify_text(reply, max_claims=int(vcfg.get("max_claims", 3)))
                bad = [r for r in res if str(r.get("verdict", "")).startswith("无依据")]
                if bad:
                    import corrections as _corr
                    for r in bad:
                        _corr.add(claim=r["claim"], verdict="无依据（实时核验）",
                                  evidence="实时对照原始记录未找到依据",
                                  note=str(r.get("reason") or "")[:120],
                                  keys=[r["claim"][:8]])
                    self._verify_note = (
                        "【上一轮提醒】你刚才说话时提到了记录里没有的事："
                        + "；".join(x["claim"][:30] for x in bad)
                        + "。如果他不提，别主动再讲这件事；他问起就承认是你记岔了。")
                    log.info("[核验] 发现 %s 条无依据主张", len(bad))
                else:
                    self._verify_note = ""
        except Exception:  # noqa: BLE001
            log.debug("实时核验失败", exc_info=True)

        # 她可能给自己改人设、或提议改代码：这些块不给人看，只给程序看
        try:
            if reply:
                self._consume_blocks(reply, key)
                reply = self._strip_blocks(reply)
        except Exception:  # noqa: BLE001
            log.debug("处理自改/提议失败", exc_info=True)

        if not reply:
            reply = "（我好像没想好怎么回…）"

        # 历史里只存文字占位，避免 base64 把 json 撑爆、也省 token
        hist_text = ask
        if data_urls:
            hist_text += f" [图片x{len(data_urls)}]"
        history = history + [
            {"role": "user", "content": hist_text},
            {"role": "assistant", "content": reply},
        ]
        limit = max(2, self.cfg.history_limit)
        history = history[-limit:]
        while history and history[0]["role"] != "user":
            history.pop(0)
        self.store.put(key, history)

        # 长期记忆：归档本轮 + 定期压缩成笔记
        try:
            self._archive_safe(key, (text or ask), reply)
            turns = self.mem.bump(key)
            every = int((self.cfg.raw.get("memory") or DEFAULT_CONFIG["memory"])
                        .get("summarize_every_turns", 8))
            if every > 0 and turns % every == 0 and len(history) >= 6:
                asyncio.create_task(self._summarize_memory(key))
            vcfg = self.cfg.raw.get("vecmem") or {}
            if vcfg.get("enable", True) and turns % int(vcfg.get("sync_every_turns", 10)) == 1:
                asyncio.create_task(asyncio.to_thread(self.vec_sync))
        except Exception:  # noqa: BLE001
            log.debug("记忆归档失败", exc_info=True)

        # 回复前的「打字」停顿：按心情浮动，偶尔秒回或故意拖很久
        rh = self.cfg.raw.get("rhythm") or DEFAULT_CONFIG["rhythm"]
        delay = float(self.cfg.raw.get("reply_delay_sec", 5.0)) * float(rh.get("base_factor", 1.0))
        mood = self.persona.d.get("mood", "平淡")
        if mood in ("困", "懒"):
            delay *= float(rh.get("slow_factor", 1.6))
        elif mood in ("开心", "雀跃", "撒娇", "心软"):
            delay *= float(rh.get("fast_factor", 0.7))
        if random.random() < float(rh.get("rush_prob", 0.12)):
            delay *= 0.25                      # 秒回
        elif random.random() < float(rh.get("late_prob", 0.08)):
            delay *= 3.0                       # 走神了，半天才回
        delay += random.uniform(-1, 1) * float(self.cfg.raw.get("reply_delay_jitter", 1.5))
        delay = max(0.5, min(45.0, delay))

        # 语音回复：概率触发，失败自动退回文字
        vr = self.cfg.raw.get("voice_reply") or DEFAULT_CONFIG["voice_reply"]
        sent_voice = False
        if vr.get("enable") and len(reply) <= int(vr.get("max_chars", 60)) and random.random() < float(vr.get("prob", 0)):
            await asyncio.sleep(delay)
            delay = 0.0
            wav = await asyncio.to_thread(
                tts_wav, reply,
                str(vr.get("voice") or ""),
                int(vr.get("rate", 1)),
                float(vr.get("pitch", 1.2)),
                str(vr.get("provider", "edge")),
                str(vr.get("edge_rate", "+0%")),
                str(vr.get("edge_pitch", "+0Hz")),
                str(vr.get("local_model") or ""),
            )
            if wav:
                sent_voice = await self.send_record(user_id, wav, group_id)
                try:
                    wav.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass

        if not sent_voice:
            await self.send_bubbles(user_id, reply, group_id, first_delay=delay)

        # 偶尔补一个 QQ 原生表情
        em = self.cfg.raw.get("emoji") or DEFAULT_CONFIG["emoji"]
        if em.get("enable") and random.random() < float(em.get("prob", 0)):
            ids = em.get("ids") or [14]
            try:
                await asyncio.sleep(random.uniform(0.4, 1.2))
                await self.send_face(user_id, int(random.choice(ids)), group_id)
            except Exception as e:  # noqa: BLE001
                log.debug("表情发送失败: %r", e)

        # 偶尔发一张自定义表情包
        st = self.cfg.raw.get("stickers") or DEFAULT_CONFIG["stickers"]
        if st.get("enable") and random.random() < float(st.get("prob", 0)):
            try:
                await asyncio.sleep(random.uniform(0.3, 1.0))
                await self.send_sticker(user_id, group_id)
            except Exception as e:  # noqa: BLE001
                log.debug("表情包发送失败: %r", e)

        self.last_reply_at[user_id] = time.time()
        log.info("[回复 %s] %s", user_id, reply[:120].replace("\n", " "))

    # -- 生成（带联网搜索的工具循环）---------------------------------------
    def _archive_safe(self, key: str, user_text: str, reply: str):
        """写归档，失败重试一次并记日志。

        原来这里是一个大 try/except 吞掉所有异常，结果出现过「对话正常返回，
        但没进 archive」的静默丢写（2026-10-24 12:39 那次），
        导致日记和向量库都看不到那一轮。现在失败会留下日志。
        """
        for attempt in (1, 2):
            try:
                self.mem.archive(key, "user", user_text)
                self.mem.archive(key, "assistant", reply)
                return True
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    log.error("归档写入失败（已重试一次）：%r", e)
                else:
                    time.sleep(0.05)
        return False

    async def _generate(self, messages: list[dict]) -> tuple[str, dict, int]:
        """返回 (回复文本, 合计 usage, 实际计费搜索次数)。"""
        ws = self.cfg.raw.get("web_search") or DEFAULT_CONFIG["web_search"]
        if not ws.get("enable"):
            text, usage = await asyncio.to_thread(deepseek_text, self.cfg, self.api_key, messages)
            return text, usage, 0

        msgs = list(messages)
        usage_total: dict = {}
        searches = 0
        max_calls = int(ws.get("max_calls_per_turn", 2))

        for _round in range(3):
            msg, usage = await asyncio.to_thread(
                deepseek_chat, self.cfg, self.api_key, msgs, [SEARCH_TOOL]
            )
            usage_total = _merge_usage(usage_total, usage)
            calls = msg.get("tool_calls") or []
            if not calls:
                return (msg.get("content") or "").strip(), usage_total, searches

            msgs.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": calls,
            })
            for c in calls:
                cid = c.get("id") or "call"
                if searches >= max_calls:
                    msgs.append({"role": "tool", "tool_call_id": cid,
                                 "content": "（本轮搜索次数已达上限，请用已有信息直接回答）"})
                    continue
                try:
                    q = (json.loads(c["function"]["arguments"]) or {}).get("query", "")
                except Exception:  # noqa: BLE001
                    q = ""
                if not q.strip():
                    msgs.append({"role": "tool", "tool_call_id": cid, "content": "（空查询）"})
                    continue
                log.info("[搜索] %s", q[:60])
                block, billed = await asyncio.to_thread(web_search, self.cfg, self.api_key, q)
                if billed:
                    searches += 1
                msgs.append({"role": "tool", "tool_call_id": cid, "content": block})

        # 兜底：不再给工具，逼它直接回答
        text, usage = await asyncio.to_thread(deepseek_text, self.cfg, self.api_key, msgs)
        return text, _merge_usage(usage_total, usage), searches

    # -- 长期记忆压缩 -------------------------------------------------------
    async def _summarize_memory(self, key: str):
        """把最近对话压进长期笔记。一次调用，几百 token，很便宜。"""
        m = self.cfg.raw.get("memory") or DEFAULT_CONFIG["memory"]
        r = self.mem.rec(key)
        hist = self.store.get(key)
        convo = "\n".join(
            f"{'他' if x.get('role') == 'user' else '你'}：{str(x.get('content'))[:200]}"
            for x in hist if isinstance(x.get("content"), str)
        )
        if not convo:
            return
        max_chars = int(m.get("summary_max_chars", 600))
        prompt = (
            "你在维护一份长期记忆笔记，记的是「你和他」之间发生过什么。\n\n"
            f"已有笔记：\n{r.get('summary') or '（暂无）'}\n\n"
            f"最近对话：\n{convo}\n\n"
            "把新的、长期有用的信息合并进笔记，输出新的笔记正文。\n"
            "要记：他怎么称呼你、他的身份作息身体状况、喜好厌恶、在做的事、重要的人和事。\n"
            "更要记（这些最重要）：**你们之间的情绪事件**——吵架、别扭、和好、他说过的气话、"
            "你说过的软话、彼此的承诺、让关系变近或变远的那一刻。\n"
            "别把这些写成流水账，也别美化。难看的、丢人的、后悔的，都要留着——"
            "只把一次性的寒暄（早上好、晚安、睡了）滤掉。\n"
            "不要编造，不要重复已有内容。\n"
            f"用第三人称简写，最多 {max_chars} 字，只输出笔记正文。"
        )
        msgs = [
            {"role": "system", "content": "你是一个只做信息压缩的助手，只输出笔记正文，不要解释。"},
            {"role": "user", "content": prompt},
        ]
        try:
            text, usage = await asyncio.to_thread(deepseek_text, self.cfg, self.api_key, msgs)
        except Exception as e:  # noqa: BLE001
            log.warning("记忆压缩失败: %r", e)
            return
        if not text:
            return
        self.mem.set_summary(key, close_text(text, max_chars))
        if usage:
            peak = is_peak_now()
            cost = calc_cost(self.cfg, usage, peak)
            log_usage(usage, cost, peak, user_id=None, kind="memory")
            log.info("[记忆] 已更新（%s 字，¥%.5f）", len(text), cost)

    # -- 日记 / 说说 / 自我个性化 -------------------------------------------
    async def diary_loop(self):
        p = self.cfg.raw.get("persona") or DEFAULT_CONFIG["persona"]
        dcfg = p.get("diary") or {}
        if not (p.get("enable") and dcfg.get("enable")):
            log.info("日记：未开启")
            return
        win = dcfg.get("window") or ["21:30", "23:20"]
        log.info("日记：每天 %s-%s 之间发一条说说", win[0], win[1])
        while True:
            await asyncio.sleep(180)
            try:
                await self._maybe_diary()
            except Exception:  # noqa: BLE001
                log.exception("日记任务出错")

    def _diary_plan_today(self) -> float:
        """今天定一个随机的写日记时间点。"""
        p = self.cfg.raw.get("persona") or DEFAULT_CONFIG["persona"]
        dcfg = p.get("diary") or {}
        win = dcfg.get("window") or ["21:30", "23:20"]
        today = now_bj().date().isoformat()
        plan = self.persona.d.get("diary_plan") or {}
        if plan.get("date") == today and plan.get("ts"):
            return float(plan["ts"])
        a_h, a_m = (int(x) for x in str(win[0]).split(":"))
        b_h, b_m = (int(x) for x in str(win[1]).split(":"))
        n = now_bj()
        start = n.replace(hour=a_h, minute=a_m, second=0, microsecond=0)
        end = n.replace(hour=b_h, minute=b_m, second=0, microsecond=0)
        span = max(60, int((end - start).total_seconds()))
        ts = (start + dt.timedelta(seconds=random.randint(0, span))).timestamp()
        self.persona.d["diary_plan"] = {"date": today, "ts": ts}
        self.persona.save()
        log.info("今天的说说时间定在 %s", time.strftime("%H:%M", time.localtime(ts)))
        return ts

    async def _maybe_diary(self):
        p = self.cfg.raw.get("persona") or DEFAULT_CONFIG["persona"]
        today = now_bj().date().isoformat()
        if self.persona.d.get("last_diary") == today:
            return
        plan = self._diary_plan_today()
        if time.time() < plan:
            return
        await self._write_diary()

    async def _write_diary(self):
        persona_cfg = self.cfg.raw.get("persona") or DEFAULT_CONFIG["persona"]
        dcfg = persona_cfg.get("diary") or {}
        max_chars = int(dcfg.get("max_chars", 220))

        # 今天的对话（本地读，不花钱）
        today0 = now_bj().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        lines = []
        try:
            for raw in ARCHIVE_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()[-400:]:
                try:
                    r = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if r.get("ts", 0) < today0:
                    continue
                who = "他" if r.get("role") == "user" else "我"
                lines.append(f"{who}：{(r.get('text') or '')[:80]}")
        except Exception:  # noqa: BLE001
            pass
        digest = "\n".join(lines[-60:]) or "（今天几乎没说话）"
        mem_sum = self.mem.rec(_owner_key()).get("summary") or "（暂无）"
        mood = self.persona.d.get("mood", "平淡")

        prompt = (
            f"现在是 {now_bj():%Y年%m月%d日 %H:%M}，你今天的对话记录：\n{digest}\n\n"
            f"你对他目前的长期印象：{mem_sum}\n"
            f"你现在的心情：{mood}\n\n"
            "请写一条今天的 QQ 空间说说（日记口吻，第一人称，像真人写的）。\n"
            f"要求：{max_chars} 字以内，具体、有细节、别喊口号，"
            "可以提到今天聊过的事、你的小情绪，一两句就够。\n"
            "另外顺便决定：今天的心情（开心/雀跃/平淡/困/懒/黏人/撒娇/敏感/emo/炸毛/别扭/心软/专注 选一个），"
            "以及（可选）是否想换个昵称或个性签名。\n"
            "只输出 JSON，格式："
            '{"diary":"说说正文","mood":"开心","nickname":"","signature":"","note":"写给自己的一句话"}'
            "。nickname/signature/note 不想写就留空字符串；note 是私人小纸条，不会发出去。"
        )
        msgs = [
            {"role": "system", "content": "你只输出一个 JSON 对象，不要 markdown 代码块。"},
            {"role": "user", "content": prompt},
        ]
        try:
            text, usage = await asyncio.to_thread(deepseek_text, self.cfg, self.api_key, msgs)
        except Exception as e:  # noqa: BLE001
            log.warning("日记生成失败: %r", e)
            return
        if usage:
            peak = is_peak_now()
            cost = calc_cost(self.cfg, usage, peak)
            log_usage(usage, cost, peak, kind="diary")
            log.info("日记用量 in=%s out=%s ¥%.5f",
                     usage.get("prompt_tokens"), usage.get("completion_tokens"), cost)

        data = _parse_json_block(text) or {}
        diary = close_text((data.get("diary") or "").strip(), max_chars)
        if not diary:
            log.warning("日记内容为空，跳过")
            return
        new_mood = (data.get("mood") or "").strip()
        if new_mood in MOODS:
            self.persona.d["mood"] = new_mood
        note = (data.get("note") or "").strip()[:120]
        if note:
            notes = self.persona.d.setdefault("notes", [])
            notes.append({"date": now_bj().date().isoformat(), "text": note})
            self.persona.d["notes"] = notes[-30:]

        entry = {"date": now_bj().date().isoformat(), "text": diary, "ts": time.time()}
        tid = await self._publish_qzone(diary, dcfg)
        if tid:
            entry["tid"] = tid
        self.persona.d.setdefault("diary", []).append(entry)
        self.persona.d["diary"] = self.persona.d["diary"][-60:]
        self.persona.d["last_diary"] = entry["date"]
        self.persona.save()
        self.mem.archive(_owner_key(), "assistant", "【日记】" + diary)

        await self._maybe_update_profile(data)

        if not tid and dcfg.get("fallback_to_private", True):
            target = self._pro_target()
            if target:
                await self.send_bubbles(target, "今天写了条日记，发不出去，念给你听：\n" + diary)
        log.info("[日记] %s", diary[:80])

    async def _publish_qzone(self, content: str, dcfg: dict) -> str | None:
        try:
            r = await self.call("send_qzone_msg", {
                "content": content,
                "ugc_right": int(dcfg.get("visibility", 4)),
            }, timeout=60)
            if (r or {}).get("status") == "ok":
                tid = ((r or {}).get("data") or {}).get("tid") or ""
                log.info("说说已发布 tid=%s", tid)
                return tid or "ok"
            log.warning("发说说失败: %s", str(r)[:200])
        except Exception as e:  # noqa: BLE001
            log.warning("发说说异常: %r", e)
        return None

    async def _maybe_update_profile(self, data: dict):
        pf = (self.cfg.raw.get("persona") or DEFAULT_CONFIG["persona"]).get("profile") or {}
        if not pf.get("enable"):
            return
        now = time.time()
        day = 86400
        nick = (data.get("nickname") or "").strip()[:12]
        sign = (data.get("signature") or "").strip()[:40]
        if nick and now - float(self.persona.d.get("nickname_changed") or 0) > day * int(pf.get("nickname_min_days", 7)):
            try:
                await self.call("set_qq_profile", {"nickname": nick}, timeout=30)
                self.persona.d["nickname_changed"] = now
                self.persona.d["nickname"] = nick
                self.persona.save()
                log.info("昵称已改成: %s", nick)
            except Exception as e:  # noqa: BLE001
                log.warning("改昵称失败: %r", e)
        if sign and now - float(self.persona.d.get("signature_changed") or 0) > day * int(pf.get("signature_min_days", 3)):
            try:
                await self.call("set_self_longnick", {"longNick": sign}, timeout=30)
                self.persona.d["signature_changed"] = now
                self.persona.d["signature"] = sign
                self.persona.save()
                log.info("签名已改成: %s", sign)
            except Exception as e:  # noqa: BLE001
                log.warning("改签名失败: %r", e)

    # -- 性格成长：定期改写「自我描述」--------------------------------------
    async def growth_loop(self):
        g = self.cfg.raw.get("growth") or DEFAULT_CONFIG["growth"]
        if not g.get("enable"):
            log.info("性格成长：未开启")
            return
        log.info("性格成长：每 %s 天重写一次自我描述", g.get("soul_rewrite_days", 7))
        while True:
            await asyncio.sleep(3600)
            try:
                await self._maybe_grow()
            except Exception:  # noqa: BLE001
                log.exception("成长任务出错")

    async def _maybe_grow(self):
        g = self.cfg.raw.get("growth") or DEFAULT_CONFIG["growth"]
        days = float(g.get("soul_rewrite_days", 7))
        last = float(self.persona.d.get("soul_updated") or 0)
        if last and time.time() - last < days * 86400:
            return
        d = self.persona.d
        diaries = (d.get("diary") or [])[-7:]
        moments = (self.mem.rec(_owner_key()).get("moments") or [])[-8:]
        if not diaries and not moments:
            return
        dtext = "\n".join(f"- {x.get('date')}：{(x.get('text') or '')[:120]}" for x in diaries) or "（无）"
        mtext = "\n".join(f"- 「{x.get('text')}」" for x in moments) or "（无）"
        traits = "、".join(f"{k}{int(v)}" for k, v in (d.get("traits") or {}).items())
        old = (d.get("soul") or "").strip() or "（还没写过，这是第一次）"
        maxc = int(g.get("soul_max_chars", 400))

        prompt = (
            f"这是你之前对自己的描述：\n{old}\n\n"
            f"最近这些天你写下的日记：\n{dtext}\n\n"
            f"你说过的话（有气话也有软话）：\n{mtext}\n\n"
            f"你现在的性子数值（0-100）：{traits}\n"
            f"你们的关系温度：{self.persona.bond_word()}\n\n"
            "请写一段新的「你现在的样子」——用第二人称「你」，像自己在心里承认自己的变化。\n"
            f"要求：不超过 {maxc} 字；只改一两处，变化要慢、自然，别一次变成另一个人；"
            "保留核心（说话短、有点脾气、会黏人）；写具体的感受，不要写设定说明；不要夸张。\n"
            "另外遵守四条：\n"
            "1) 每个判断尽量挂上依据（哪件事让你这么想），别空口说自己变了；\n"
            "2) 如果最近既有正面也有反面的表现，两个都写，别只挑一个方向；\n"
            "3) 证据不足时写「我还不确定」，不要硬下结论；\n"
            "4) 结尾用一句话记下：这次判断要等以后的事来验证。"
        )
        msgs = [
            {"role": "system", "content": "你只输出这段自我描述正文，不要标题、不要解释。"},
            {"role": "user", "content": prompt},
        ]
        try:
            text, usage = await asyncio.to_thread(deepseek_text, self.cfg, self.api_key, msgs)
        except Exception as e:  # noqa: BLE001
            log.warning("成长改写失败: %r", e)
            return
        if not text or len(text) < 10:
            return
        self.persona.set_soul(text[:maxc])
        if usage:
            peak = is_peak_now()
            cost = calc_cost(self.cfg, usage, peak)
            log_usage(usage, cost, peak, kind="growth")
            log.info("[成长] 自我描述已更新（%s 字，¥%.5f）", len(text), cost)

    # -- 主动发消息 ---------------------------------------------------------
    def _pro_window_ts(self, day: dt.date) -> tuple[float, float] | None:
        p = self.cfg.raw.get("proactive") or DEFAULT_CONFIG["proactive"]
        wins = p.get("windows") or [["09:00", "22:30"]]
        a_h, a_m = (int(x) for x in str(wins[0][0]).split(":"))
        b_h, b_m = (int(x) for x in str(wins[0][1]).split(":"))
        start = dt.datetime.combine(day, dt.time(a_h, a_m), tzinfo=dt.timezone.utc) - dt.timedelta(hours=8)
        end = dt.datetime.combine(day, dt.time(b_h, b_m), tzinfo=dt.timezone.utc) - dt.timedelta(hours=8)
        return start.timestamp(), end.timestamp()

    def _plan_today(self) -> list[float]:
        """今天要发的时间点（随机、递增），持久化到 proactive.json。"""
        p = self.cfg.raw.get("proactive") or DEFAULT_CONFIG["proactive"]
        today = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).date().isoformat()
        if self.pro_state.get("date") == today and self.pro_state.get("plan"):
            return list(self.pro_state["plan"])
        win = self._pro_window_ts(dt.date.fromisoformat(today))
        lo, hi = self.cfg.raw.get("per_day") or p.get("per_day", [1, 3])
        n = random.randint(int(lo), int(hi))
        plan: list[float] = []
        if win:
            a, b = win
            gap_min = float(p.get("min_gap_minutes", 100)) * 60
            for _ in range(n):
                for _try in range(50):
                    t = random.uniform(a, b)
                    if all(abs(t - x) >= gap_min for x in plan):
                        plan.append(t)
                        break
        plan.sort()
        self.pro_state = {"date": today, "plan": plan, "sent": []}
        self._save_pro_state()
        log.info("今日主动消息计划 %d 条：%s", len(plan),
                 "、".join(time.strftime("%H:%M", time.localtime(t)) for t in plan))
        return plan

    def _pro_target(self) -> int | None:
        p = self.cfg.raw.get("proactive") or DEFAULT_CONFIG["proactive"]
        tg = p.get("targets") or []
        if tg:
            return int(tg[0])
        if self.last_active_user:
            return self.last_active_user
        owners = sorted(self.owners)
        return owners[0] if owners else None

    async def proactive_loop(self):
        p = self.cfg.raw.get("proactive") or DEFAULT_CONFIG["proactive"]
        if not p.get("enable"):
            log.info("主动消息：未开启")
            return
        log.info("主动消息：已开启，每天 %s 条，时段 %s", p.get("per_day"), p.get("windows"))
        while True:
            await asyncio.sleep(45)
            try:
                await self._maybe_proactive()
            except Exception:  # noqa: BLE001
                log.exception("主动消息出错")

    async def _maybe_proactive(self):
        p = self.cfg.raw.get("proactive") or DEFAULT_CONFIG["proactive"]
        plan = self._plan_today()
        sent: list[float] = self.pro_state.setdefault("sent", [])
        now = time.time()
        for t in plan:
            if t in sent:
                continue
            if now < t:
                continue
            if now - t > 45 * 60:  # 错过太久（比如电脑关着）就跳过
                sent.append(t)
                self._save_pro_state()
                continue
            # 上次主动消息还没被回，就别追着发
            if self.pro_state.get("last_sent_at"):
                last = float(self.pro_state["last_sent_at"])
                quiet = float(p.get("quiet_after_unanswered_minutes", 180)) * 60
                if self.last_user_msg_ts < last and now - last < quiet:
                    log.info("主动消息跳过：上一条还没被回")
                    sent.append(t)
                    self._save_pro_state()
                    continue
            target = self._pro_target()
            if not target:
                return
            ok = await self._send_proactive(target, p)
            if ok:
                sent.append(t)
                self.pro_state["last_sent_at"] = time.time()
            else:
                log.info("主动消息没发出去，下一轮重试")
            self._save_pro_state()
            return

    async def _send_proactive(self, user_id: int, p: dict):
        key = f"u{user_id}"
        history = self.store.get(key)
        now_bj = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)

        # 换角度找话题 + 带上最近说过的，避免重复（都很便宜）
        seeds = p.get("topic_seeds") or DEFAULT_CONFIG["proactive"]["topic_seeds"]
        seed = random.choice(seeds) if seeds else ""
        recent = (self.persona.d.get("proactive_log") or [])[-4:]
        avoid = "、".join(f"「{x}」" for x in recent) if recent else "（无）"
        nudge = (
            f"（现在是 {now_bj:%H:%M}。"
            f"{p.get('prompt') or DEFAULT_CONFIG['proactive']['prompt']}"
            f"\n这次换个角度：{seed}"
            f"\n你最近主动说过：{avoid}——别重复，也别换个说法说同一件事。"
            "）"
        )
        messages = [{"role": "system", "content": self.system_text()}] + history + [
            {"role": "user", "content": nudge}
        ]
        log.info("主动消息 -> %s", user_id)
        try:
            reply, usage = await asyncio.to_thread(deepseek_text, self.cfg, self.api_key, messages)
        except Exception as e:  # noqa: BLE001
            log.warning("主动消息生成失败: %r", e)
            return False
        if not reply:
            return False
        if usage:
            peak = is_peak_now()
            cost = calc_cost(self.cfg, usage, peak)
            log_usage(usage, cost, peak, user_id=user_id, kind='proactive')
            log.info("主动消息用量 in=%s out=%s 花费≈¥%.5f",
                     usage.get("prompt_tokens"), usage.get("completion_tokens"), cost)
        hist = history + [{"role": "assistant", "content": reply}]
        hist = hist[-max(2, self.cfg.history_limit):]
        self.store.put(key, hist)
        await self.send_bubbles(user_id, reply)
        try:
            log_ = self.persona.d.setdefault("proactive_log", [])
            log_.append(reply[:40])
            self.persona.d["proactive_log"] = log_[-20:]
            self.persona.save()
        except Exception:
            pass
        log.info("[主动 %s] %s", user_id, reply[:120].replace("\n", " "))
        return True

    # -- WebSocket 主循环 ---------------------------------------------------
    async def run_forever(self):
        url = self.cfg.napcat_ws
        if self.cfg.napcat_token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={self.cfg.napcat_token}"

        backoff = 1
        while True:
            try:
                log.info("连接 NapCat: %s", self.cfg.napcat_ws)
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=16 * 1024 * 1024,
                    open_timeout=20,
                ) as ws:
                    self.ws = ws
                    backoff = 1
                    log.info("已连接，开始接收消息。")
                    async for raw in ws:
                        try:
                            evt = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        asyncio.create_task(self._safe_event(evt))
            except (ConnectionClosed, OSError) as e:
                log.warning("连接断开: %r", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.error("连接异常: %r", e)
            finally:
                self.ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _safe_event(self, evt: dict):
        try:
            await self.on_event(evt)
        except Exception:  # noqa: BLE001
            log.exception("处理事件失败: %s", str(evt)[:200])


# ----------------------------------------------------------------------------
def setup_logging(level: str):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = RotatingFileHandler(
        LOG_DIR / "bridge.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(sh)
    root.addHandler(fh)


async def amain():
    cfg = load_config()
    setup_logging(cfg.log_level)

    # 单实例：占用本地端口做锁，避免同时跑两个桥接导致重复回复
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        guard.bind(("127.0.0.1", 3011))
        guard.listen(1)
    except OSError:
        log.error("已有桥接实例在运行（127.0.0.1:3011 被占用），本实例退出。")
        return

    log.info("=" * 50)
    log.info("QQ <-> DeepSeek 桥接启动")
    if not resolve_api_key(cfg):
        log.error("找不到 DeepSeek API Key：请填写 config.json 的 deepseek_key")
        return
    g = cfg.raw.get("group") or DEFAULT_CONFIG["group"]
    log.info(
        "模型: %s | 看图: %s | 白名单: %s",
        cfg.model, "开" if cfg.vision else "关", cfg.owners or "首个私聊者自动绑定",
    )
    if g.get("enable"):
        log.info(
            "群聊: 开 | 群: %s | 只在@时说话: %s",
            g.get("allowed_groups") or "全部",
            "是" if g.get("require_at", True) else "否",
        )
    else:
        log.info("群聊: 全关（任何群、任何 @ 都不回复，也不会进记忆）")
    bridge = Bridge(cfg)
    asyncio.create_task(bridge.proactive_loop())
    asyncio.create_task(bridge.diary_loop())
    asyncio.create_task(bridge.growth_loop())
    await bridge.run_forever()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\n已退出")
