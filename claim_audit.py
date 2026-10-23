"""主张核验：她说的每一件"具体发生过的事"，能不能在记录里找到出处。

背景：示例——智能体声称发生过某件事，但归档记录里从来没有这件事。
这是生成时的细节幻觉，而且会被存档、入索引，变成假记忆。

用法：
    python claim_audit.py            扫她的所有回信/日记，列出无法核实的主张
    python claim_audit.py --days 3   只看最近 3 天
"""

from __future__ import annotations
import json
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent
DATA = BASE / "data"

# 时间/事件类陈述的特征词：出现这些，说明她在指认一件具体发生过的事
TIME_WORDS = ("昨晚", "昨天", "前天", "那天", "上次", "刚才", "今晚", "今早",
              "那天晚上", "上回", "上周", "上个月", "前几天", "前阵子",
              "上午", "下午", "早上", "昨天下午", "昨天晚上", "这两天", "这几天")
# 没有时间词，但带过去时标记的具体陈述（"你说你……了""我记着你……过"）
EVENT_PATTERNS = ("你说你", "你说过", "我记着你", "你跟我说", "你告诉过我", "你答应过")
# 明显是「她自己的判断/感受」，不需要核实
SOFT_WORDS = ("我觉得", "我想", "我猜", "可能", "也许", "大概", "我不确定", "我说不准")


def load_texts(days: int | None = None) -> list[dict]:
    """收集她说过/写过的所有文本。"""
    out = []
    cutoff = (time.time() - days * 86400) if days else 0

    # 1) 回信
    for f in sorted((DATA / "letters_to_dad").glob("*.md")):
        try:
            st = f.stat().st_mtime
        except OSError:
            continue
        if st < cutoff:
            continue
        out.append({"src": f"回信/{f.name}", "text": f.read_text(encoding="utf-8", errors="ignore")})

    # 2) 日记
    try:
        p = json.loads((DATA / "persona.json").read_text(encoding="utf-8-sig"))
        for x in (p.get("diary") or []):
            out.append({"src": f"日记/{x.get('date')}", "text": x.get("text") or ""})
    except Exception:
        pass

    # 3) 归档里她的回复
    af = DATA / "archive.jsonl"
    if af.exists():
        for ln in af.read_text(encoding="utf-8", errors="ignore").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            t = o.get("text") or ""
            # 归档形如 "他：xxx　你：yyy"，取「你：」之后
            if "你：" in t:
                her = t.split("你：", 1)[1]
                out.append({"src": f"归档/{o.get('date') or ''}", "text": her})
    return out


def corpus() -> str:
    """所有「用户侧」文本，用来判断她的主张有没有出处。"""
    parts = []
    for name in ("shared.jsonl", "diary_offline.jsonl"):
        f = DATA / name
        if f.exists():
            parts.append(f.read_text(encoding="utf-8", errors="ignore"))
    af = DATA / "archive.jsonl"
    if af.exists():
        for ln in af.read_text(encoding="utf-8", errors="ignore").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            t = o.get("text") or ""
            if "你：" in t:
                t = t.split("你：", 1)[0]      # 只要「他：」那部分
            parts.append(t)
    return "\n".join(parts)


def extract_claims(text: str) -> list[str]:
    """抽出带时间词的具体主张（一句一句看）。"""
    claims = []
    for sent in re.split(r"[。！？\n]", text):
        s = sent.strip()
        if len(s) < 6:
            continue
        if any(w in s for w in SOFT_WORDS):
            continue
        if any(w in s for w in TIME_WORDS):
            claims.append(s)
            continue
        # 没有时间词，但有"你说你…了/过"这类过去时陈述，也算具体主张
        if any(p in s for p in EVENT_PATTERNS) and ("了" in s or "过" in s):
            claims.append(s)
    return claims


def best_support(claim: str) -> float:
    """用本地向量模型，看这条主张在「他说的 / 转述的」记录里最高相似度是多少。

    比关键词匹配可靠：像「具体事件名」这种词，关键词法会被常见二字组骗过。
    """
    try:
        import vecmem
        idx = vecmem.VecIndex()
        if not idx.load():
            return -1.0
        # 只拿「对话原文」和「转述」两类做支撑证据，避免用自己的话证明自己
        allowed = {m["id"] for m in idx.meta if (m.get("type") or "") in ("chat", "told")}
        if not allowed:
            return -1.0
        hits = idx.search(claim, k=1, threshold=0.0, allowed_ids=allowed)
        return float(hits[0]["score"]) if hits else 0.0
    except Exception:  # noqa: BLE001
        return -1.0


def keywords(claim: str) -> list[str]:
    """从主张里抽关键词（去掉时间词和虚词），用于去语料里找线索。"""
    s = claim
    for w in TIME_WORDS:
        s = s.replace(w, "")
    grams = set()
    # 二字组 + 三字组
    for n in (2, 3):
        for i in range(len(s) - n + 1):
            g = s[i:i + n]
            if g.strip():
                grams.add(g)
    stop = {"你的", "我的", "他那", "时候", "什么", "不是", "就是", "因为", "所以",
            "那次", "你说", "说过", "发生", "真的", "假的", "还是", "可以", "没有"}
    return [g for g in grams if g not in stop]


def llm_judge(claim: str, evidences: list[str]) -> tuple[str, str]:
    """让模型判断：这条主张有没有被证据支持。返回 (判定, 理由)。"""
    import bridge
    cfg = bridge.load_config()
    key = bridge.resolve_api_key(cfg)
    ev = "\n".join(f"- {e[:220]}" for e in evidences) or "（无）"
    ask = (f"她说过一句话：「{claim}」\n\n"
           f"他在此之前的原始记录里，能查到这些内容：\n{ev}\n\n"
           "判断这句话里提到的具体事件，是否被记录支持。只回答三选一：\n"
           "有依据 / 无依据 / 部分有\n"
           "然后在第二行给一句不超过 30 字的理由。")
    try:
        reply, _ = bridge.deepseek_text(cfg, key, [
            {"role": "system", "content": "你是严格的事实核对员。只判断，不客套。"},
            {"role": "user", "content": ask}])
        lines = [x.strip() for x in (reply or "").splitlines() if x.strip()]
        verdict = lines[0] if lines else "未知"
        reason = lines[1] if len(lines) > 1 else ""
        return verdict[:20], reason[:60]
    except Exception as e:  # noqa: BLE001
        return "判断失败", str(e)[:40]


# 事后自我更正类文本的特征（对照组要排除它们，模拟"更正之前"的状态）
CORRECTION_MARKS = ("脑补", "我没摔过", "没有摔过鼠标", "是我自己加的", "记岔了", "编的")


def top_evidence(claim: str, k: int = 3, include_self: bool = False,
                 drop_corrections: bool = False) -> list[str]:
    """检索候选证据。

    include_self=False（默认）：只取用户侧记录（chat / told），排除她自己生成的内容。
    include_self=True：对照组用——允许她自己的话作为证据。
    """
    try:
        import vecmem
        idx = vecmem.VecIndex()
        if not idx.load():
            return []
        if include_self:
            allowed = None
        else:
            allowed = {m["id"] for m in idx.meta
                       if (m.get("type") or "") in ("chat", "told")}
        if drop_corrections:
            bad = {m["id"] for m in idx.meta
                   if any(w in (m.get("text") or "") for w in CORRECTION_MARKS)}
            allowed = (set(m["id"] for m in idx.meta) - bad) if allowed is None else (allowed - bad)
        hits = idx.search(claim, k=k, threshold=0.0, allowed_ids=allowed)
        return [h.get("text") or "" for h in hits]
    except Exception:  # noqa: BLE001
        return []


def verify_text(text: str, max_claims: int = 3, include_self: bool = False,
                drop_corrections: bool = False) -> list[dict]:
    """实时核验一段回复。

    不做语义预筛（会被"他说没发生过"这种否定句骗过），
    直接把带时间词的句子交给一次批量判断，成本约 ¥0.002/轮，且只在有具体主张时才调用。
    """
    claims = extract_claims(text or "")[:max_claims]
    if not claims:
        return []

    import bridge
    cfg = bridge.load_config()
    key = bridge.resolve_api_key(cfg)
    blocks = []
    for i, c in enumerate(claims, 1):
        ev = top_evidence(c, k=3, include_self=include_self, drop_corrections=drop_corrections)
        ev_txt = "\n".join(f"    - {e[:180]}" for e in ev) or "    （无）"
        blocks.append(f"{i}. 她说的：「{c[:120]}」\n   记录里查到：\n{ev_txt}")
    ask = ("下面是她说的话，以及系统在原始记录里查到的内容。\n\n"
           + "\n\n".join(blocks)
           + "\n\n判断每一条：她话里提到的具体事件，记录里有没有依据。\n"
             "注意：如果记录里是在否认这件事（例如对方说\"我没摔过\"），那算无依据。\n"
             "严格按这个格式，每条一行，别的话不要：\n"
             "序号|有依据 或 无依据 或 部分有|不超过20字理由")
    try:
        reply, _ = bridge.deepseek_text(cfg, key, [
            {"role": "system", "content": "你是严格的事实核对员，只判断，不客套，不替她圆场。"},
            {"role": "user", "content": ask}])
    except Exception as e:  # noqa: BLE001
        return [{"claim": c, "verdict": "判断失败", "reason": str(e)[:40]} for c in claims]

    parsed = {}
    for ln in (reply or "").splitlines():
        if "|" not in ln:
            continue
        parts = [x.strip() for x in ln.split("|")]
        try:
            idx = int(parts[0].strip(" .、)"))
        except Exception:
            continue
        parsed[idx] = (parts[1], parts[2] if len(parts) > 2 else "")
    out = []
    for i, c in enumerate(claims, 1):
        v, r = parsed.get(i, ("未知", ""))
        out.append({"claim": c, "verdict": v, "reason": r})
    return out


def main():
    args = sys.argv[1:]
    days = int(args[args.index("--days") + 1]) if "--days" in args else None
    texts = load_texts(days)
    body = corpus()
    print(f"扫描 {len(texts)} 篇她的文本，对照 {len(body)} 字用户侧语料\n")

    total_claims = 0
    suspicious = []
    for item in texts:
        for c in extract_claims(item["text"]):
            total_claims += 1
            score = best_support(c)
            if 0 <= score < 0.60:      # 先按语义筛一遍，可疑的再逐条核对
                ev = top_evidence(c)
                verdict, reason = llm_judge(c, ev)
                if verdict.startswith("无依据"):
                    suspicious.append((item["src"], c, f"{score:.2f} / {verdict}：{reason}"))

    print(f"含时间词的主张：{total_claims} 条")
    print(f"无法在记录中找到出处的：{len(suspicious)} 条\n")
    if not suspicious:
        print("✅ 没有发现问题主张。")
        return 0
    for src, c, r in suspicious:
        print(f"  ⚠️ [{src}] {r}")
        print(f"      {c[:100]}")
    print("\n说明：匹配度低只是「可疑」，不是判定造假。人工再核一遍。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
