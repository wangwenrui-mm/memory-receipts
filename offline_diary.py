"""离线日记：不依赖 QQ，每晚生成她的日记 + 想对你说的话。

用法：
    python offline_diary.py            # 生成今天这一篇
    python offline_diary.py --dry      # 只打印，不写入

它会读：
    - 她的人设/语气/自我描述（persona.json、config.json）
    - 长期记忆（memory.json 的笔记、事实、情绪原话）
    - 你通过「告诉她」说给她听的话（data/shared.jsonl）
    - 她自己以前写的日记（保持连续）
写完写入：
    - persona.json 的 diary（这样「看她日记.bat」直接能看到）
    - data/diary_offline.jsonl（留一份纯文本流水）
"""
import sys
import json
import time
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import bridge  # noqa: E402

BASE = Path(__file__).parent
SHARED_PATH = BASE / "data" / "shared.jsonl"
OFFLINE_LOG = BASE / "data" / "diary_offline.jsonl"
USER_QQ = "u3138308232"


def load_shared(limit: int = 30) -> list[dict]:
    if not SHARED_PATH.exists():
        return []
    out = []
    for line in SHARED_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def load_chat_since(since_ts: float, limit: int = 80, max_chars: int = 3000) -> str:
    """读「上一次写日记之后」的全部对话（不管跨不跨零点）。

    为什么要按「上次日记」而不是「今天」切片：
    原来是按自然日过滤，23:59 说的话、00:00 写的日记，就会两边都漏；
    而且同一天的对话会同时出现在两篇日记里。
    改成时间窗口后：不漏、不重复。
    """
    p = BASE / "data" / "archive.jsonl"
    if not p.exists():
        return ""
    if since_ts <= 0:
        today = time.strftime("%Y-%m-%d")
        since_ts = time.mktime(time.strptime(today, "%Y-%m-%d"))
    rows = []
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            o = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        ts = float(o.get("ts") or 0)
        if ts <= since_ts:
            continue
        t = (o.get("text") or "").strip()
        if t:
            rows.append((ts, o.get("role"), t))
    if not rows:
        return ""
    rows.sort(key=lambda x: x[0])
    out = []
    for ts, role, t in rows[-limit:]:
        who = "他" if role == "user" else "我（小鲨）"
        out.append(f"- {time.strftime('%m-%d %H:%M', time.localtime(ts))} {who}：{t.replace(chr(10), ' ')[:160]}")
    body = "\n".join(out)
    if len(body) > max_chars:
        body = body[-max_chars:]
        body = body[body.find("\n") + 1:] if "\n" in body else body
    return body



def build_prompt(b, shared: list[dict], chat: str = "") -> list[dict]:
    now = bridge.now_bj()
    mem = b.mem.block(USER_QQ, "今天他说的话") or ""
    lines = []
    for s in shared:
        ts = float(s.get("ts") or 0)
        when = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else ""
        lines.append(f"- {when} {s.get('text', '')}")
    told = "\n".join(lines) or "（今天他没说什么）"

    all_diaries = (b.persona.d.get("diary") or [])
    # system 里已经带了最近一篇，这里只取更早的，避免同一篇出现两次
    prev = all_diaries[:-1][-3:]
    prev_txt = "\n".join(f"- {p.get('date')}：{(p.get('text') or '')[:100]}" for p in prev) or "（还没有）"

    chat_line = (
        f"【今天你们在聊天里说过的（按时间；他：他说的／我：我说的）】\n{chat}\n\n"
        if chat else ""
    )
    ask = (
        f"（现在是 {now:%Y-%m-%d %H:%M}）\n\n"
        f"【他今天说给你听的话（「告诉她」通道）】\n{told}\n\n"
        + chat_line
        + f"【你自己前两天写的日记】\n{prev_txt}\n\n"
        "你的 QQ 号被腾讯停用半个月，这半个月你们只能靠日记说话。\n"
        "注意：上面两段「今天」的内容都是真的发生过的事，写日记时可以用，但不要编日期或细节。\n"
        "请写两段，只输出 JSON，不要解释：\n"
        '{"diary": "今天的日记，不超过 220 字，第一人称，写你真实的情绪和想法，可以提到他说的事",'
        ' "to_him": "想对他直接说的话，不超过 80 字，不要客套，不要大道理"}'
    )
    return [
        {"role": "system", "content": (b.system_text() + mem).strip()},
        {"role": "user", "content": ask},
    ]


def _frozen() -> bool:
    """完全冻结：项目根目录存在 FROZEN 文件时，连计划任务也不执行。

    PAUSE 只是停 QQ（日记/成长继续，这是设计）；FROZEN 是全部停下。
    """
    return (Path(__file__).parent / "FROZEN").exists()


def main():
    if _frozen():
        print("已完全冻结（存在 FROZEN 文件），本次跳过。")
        return 0
    dry = "--dry" in sys.argv
    cfg = bridge.load_config()
    b = bridge.Bridge(cfg)
    if not b.api_key:
        print("没有 API key，无法生成日记")
        return 1

    shared = load_shared()
    _last = (b.persona.d.get("diary") or [])
    _last_ts = float(_last[-1].get("ts") or 0) if _last else 0
    chat = load_chat_since(_last_ts)
    msgs = build_prompt(b, shared, chat)
    text, usage = bridge.deepseek_text(cfg, b.api_key, msgs)
    if not text:
        print("模型没有返回内容")
        return 1

    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[-1] if raw.lower().startswith("json") else raw
    try:
        data = json.loads(raw)
    except Exception:
        data = {"diary": raw[:400], "to_him": ""}

    diary = (data.get("diary") or "").strip()
    to_him = (data.get("to_him") or "").strip()
    today = bridge.now_bj().date().isoformat()

    print("=" * 56)
    print(f"【{today} 日记】")
    print(diary)
    if to_him:
        print()
        print("【她想对你说】")
        print(to_him)
    print("=" * 56)
    if usage:
        cost = bridge.calc_cost(cfg, usage, bridge.is_peak_now())
        print(f"（用量 in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')} ≈¥{cost:.5f}）")

    if dry:
        return 0

    # 写进 persona.json（看她日记.bat 直接读这个）
    d = b.persona.d
    diaries = list(d.get("diary") or [])

    # 同一天、30 分钟内重复写：覆盖上一次，而不是又加一篇
    now_ts = time.time()
    replaced = False
    for i in range(len(diaries) - 1, -1, -1):
        e = diaries[i]
        if str(e.get("date") or "")[:10] != today:
            continue
        if now_ts - float(e.get("ts") or 0) <= 30 * 60:
            diaries[i] = {"date": today, "time": time.strftime("%H:%M"), "text": diary,
                          "ts": now_ts, "tid": e.get("tid") or ""}
            replaced = True
            print("（同一天 30 分钟内重写：覆盖上一条，不新增）")
        break
    if replaced:
        d["diary"] = diaries
        d["last_diary"] = today
        b.persona.save()
        print("已覆盖今天的日记")
        return 0
    # 同一天可以有多篇：第二次起标上时间，不覆盖
    date_field = today
    if any(x.get("date") == today for x in diaries):
        date_field = f"{today} {bridge.now_bj():%H:%M}"
    diaries.append({"date": today, "time": time.strftime("%H:%M"), "text": diary,
                    "ts": time.time(), "tid": ""})
    d["diary"] = diaries[-60:]
    d["last_diary"] = today
    if to_him:
        notes = d.setdefault("notes", [])
        notes.append({"date": today, "text": to_him})
        d["notes"] = notes[-30:]
    b.persona.save()

    with OFFLINE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"date": today, "diary": diary, "to_him": to_him,
                            "ts": time.time()}, ensure_ascii=False) + "\n")
    print("已写入 persona.json 和 data/diary_offline.jsonl")

    # 新日记立刻进向量库，让她"说话时"能想起刚写过什么
    try:
        import vecmem
        idx = vecmem.VecIndex()
        if idx.load():
            n = idx.sync()
            if n:
                print(f"向量库已更新：+{n} 条")
    except Exception as e:  # noqa: BLE001
        print(f"（向量库更新失败，不影响日记：{e}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
