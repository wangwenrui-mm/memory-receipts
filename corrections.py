"""更正登记簿：她说过但事实不成立的内容，以及更正后的状态。

为什么需要：
示例：智能体声称发生过某件事，但记录里从来没有这件事。
这句错话已经被存档、切块、进了索引——如果不标记，
下次检索会把"她自己编的"当成"他说过的"，形成假记忆。

做法：
- 每条更正记录：错误主张、判定、依据、日期
- 检索命中时，如果文本和她曾编过的主张高度重合，就在结果里加一句
  "（这条你后来自己承认是补的细节）"
- 只读、可审计，不改她的原文
"""

from __future__ import annotations
import json
import time
import difflib
from pathlib import Path

BASE = Path(__file__).parent
DATA = BASE / "data"
PATH = DATA / "corrections.jsonl"


def _load() -> list[dict]:
    if not PATH.exists():
        return []
    out = []
    for ln in PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def add(claim: str, verdict: str, evidence: str = "", note: str = "", keys: list[str] | None = None) -> dict:
    """登记一条更正。claim 是那句错话（或它的关键片段）。"""
    DATA.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "date": time.strftime("%Y-%m-%d %H:%M"),
           "claim": claim.strip()[:200], "keys": [k for k in (keys or []) if k],
           "verdict": verdict,
           "evidence": evidence[:300], "note": note[:200]}
    items = _load()
    items.append(rec)
    with PATH.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    return rec


def match(text: str, ratio: float = 0.55) -> str | None:
    """这段文本是不是踩到了某条已更正的错话？返回提示语。"""
    t = (text or "").strip()
    if not t:
        return None
    for rec in _load():
        claim = rec.get("claim") or ""
        for k in (rec.get("keys") or []):
            if k and k in t:
                return f"（注意：这句里提到的「{k}」是假的，你自己后来承认是补的细节）"
        if not claim:
            continue
        # 短片段直接包含判断；长文本用相似度
        if len(claim) <= 12:
            if claim in t:
                return f"（注意：这句里提到的「{claim}」是假的，你自己后来承认是补的细节）"
        else:
            if difflib.SequenceMatcher(None, claim, t).ratio() > ratio:
                return "（注意：这条你后来自己承认是补的细节，事实里没有这回事）"
    return None


def stats() -> dict:
    items = _load()
    return {"count": len(items),
            "size_kb": round(PATH.stat().st_size / 1024, 1) if PATH.exists() else 0}


def main():
    import sys
    if "--list" in sys.argv:
        print("更正记录：", stats())
        for r in _load():
            print(f"  [{r['date']}] {r['verdict']}  {r['claim'][:70]}")
            if r.get("note"):
                print(f"            备注：{r['note']}")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
