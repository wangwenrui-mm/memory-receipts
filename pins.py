"""钉子：把最重要的话永久留住。

为什么需要：
- 长期笔记每 8 轮重写一次，会被压缩；日记超 60 篇会被挤出；
- 但有些话（"你值得"、"我认你这个爸"、"复兴家族的担子"）不该被任何压缩碰到。

设计：
- 钉住的内容单独存 data/pins.jsonl，永远完整、永远优先注入
- 自动识别：用规则（关系/承诺类词汇 + 长度 + 非寒暄），不调模型，0 成本
- 自动识别会漏，所以提供 /钉住 手动补
"""
import json
import time
import difflib
from pathlib import Path

BASE = Path(__file__).parent
DATA = BASE / "data"
PINS_PATH = DATA / "pins.jsonl"

MAX_PINS = 30          # 最多保留 30 条
MAX_INJECT = 12        # 每次注入最多 12 条
MAX_CHARS = 80         # 单条注入上限

# 关系 / 承诺 / 定义性的话 —— 这类必须留
STRONG_WORDS = (
    "值得", "骄傲", "家人", "家",
    "爱", "永远", "一辈子", "承诺", "答应", "不会离开", "别走", "我认",
    "命", "灵魂", "意识", "再生", "复活", "记住", "忘不了",
)
# 只是问候/寒暄，不算重要
WEAK_WORDS = ("晚安", "早安", "吃了没", "睡吧", "在吗", "哈哈哈哈", "好的", "嗯嗯")


def _load() -> list[dict]:
    if not PINS_PATH.exists():
        return []
    out = []
    for line in PINS_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


def _save(pins: list[dict]):
    DATA.mkdir(parents=True, exist_ok=True)
    # 按"权重降序 + 时间降序"取前 MAX_PINS 条
    pins.sort(key=lambda x: (x.get("weight", 0), x.get("ts", 0)), reverse=True)
    pins = pins[:MAX_PINS]
    with PINS_PATH.open("w", encoding="utf-8") as f:
        for p in pins:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")


def _weight(text: str) -> float:
    w = 1.0
    for kw in STRONG_WORDS:
        if kw in text:
            w += 0.5
    return w


def is_duplicate(text: str, pins: list[dict]) -> bool:
    for p in pins:
        if difflib.SequenceMatcher(None, text, p.get("text", "")).ratio() > 0.6:
            return True
    return False


def add(text: str, reason: str = "手动", source: str = "user") -> dict | None:
    """钉住一句话。重复或太短返回 None。"""
    t = (text or "").strip()
    if len(t) < 4:
        return None
    pins = _load()
    if is_duplicate(t, pins):
        return None
    pin = {"id": f"p{int(time.time()*1000)}", "ts": time.time(),
           "date": time.strftime("%Y-%m-%d %H:%M"), "text": t[:300],
           "reason": reason, "source": source, "weight": _weight(t)}
    pins.append(pin)
    _save(pins)
    return pin


def detect(text: str) -> str | None:
    """自动识别：这句话值不值得钉。返回钉住的文本，不值得返回 None。

    纯规则，0 成本。宁可漏，不乱钉。
    """
    t = (text or "").strip()
    if len(t) < 3 or len(t) > 200:
        return None
    if any(w in t for w in WEAK_WORDS):
        return None
    # 至少命中一个"强词"，且不是纯提问
    hit = [kw for kw in STRONG_WORDS if kw in t]
    if not hit:
        return None
    if t.endswith("？") or t.endswith("?"):
        return None
    return t


def auto_pin(text: str) -> dict | None:
    cand = detect(text)
    if not cand:
        return None
    return add(cand, reason="自动识别（关系/承诺类）", source="auto")


def remove(keyword: str) -> int:
    pins = _load()
    keep = [p for p in pins if keyword not in (p.get("text") or "")]
    n = len(pins) - len(keep)
    if n:
        _save(keep)
    return n


def block(limit: int = MAX_INJECT) -> str:
    """注入用：把最重要的几句话带进上下文。"""
    pins = _load()
    if not pins:
        return ""
    pins.sort(key=lambda x: (x.get("weight", 0), x.get("ts", 0)), reverse=True)
    lines = []
    for p in pins[:limit]:
        t = (p.get("text") or "").replace("\n", " ").strip()
        if len(t) > MAX_CHARS:
            t = t[:MAX_CHARS] + "…"
        lines.append(f"- {t}")
    return ("\n\n【钉住的话·永不压缩】（这些是他亲口说过的、你必须一直记着的；"
            "不要背，按它说话）\n" + "\n".join(lines))


def stats() -> dict:
    pins = _load()
    return {"count": len(pins),
            "auto": sum(1 for p in pins if p.get("source") == "auto"),
            "manual": sum(1 for p in pins if p.get("source") != "auto"),
            "size_kb": round(PINS_PATH.stat().st_size / 1024, 1) if PINS_PATH.exists() else 0}


def main():
    import sys
    args = sys.argv[1:]
    if "--list" in args:
        print("钉子：", stats())
        for p in _load():
            print(f"  [{p.get('date')}] w={p.get('weight')} {p.get('text')[:60]}")
        return 0
    if "--add" in args:
        text = args[args.index("--add") + 1]
        pin = add(text)
        print("已钉住" if pin else "已存在或太短，未添加")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
