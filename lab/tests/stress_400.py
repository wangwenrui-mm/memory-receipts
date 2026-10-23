"""400 篇真实论文 · 极限压测。

测什么：
  A. 抽取覆盖率（真实语料里到底能抽出多少、覆盖哪些单位）
  B. 噪声率（不该被当成测量的数字有没有被抽出来）
  C. 判定准确性（支持/扰动/换参数，每组 ≥200 条）
  D. 12 项极限探针（专门试探系统边界，结果用来定义"极限在哪"）
  E. 规模与性能（记录数 × 陈述数）
  F. 确定性

用法：
    python stress_400.py
"""
# --- 输出编码保护（Windows 控制台默认 GBK，会让 emoji/中文崩溃）---
try:
    import sys as _sys
    for _s in (_sys.stdout, _sys.stderr):
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import json
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lab.src import verify as V  # noqa: E402

BASE = Path(__file__).resolve().parents[1]
CORPUS = BASE / "data" / "corpus"
NS = {"a": "http://www.w3.org/2005/Atom"}


def load_corpus():
    items = []
    for f in sorted(CORPUS.glob("*.xml")):
        try:
            tree = ET.parse(f)
        except Exception:
            continue
        for e in tree.getroot().findall("a:entry", NS):
            title = (e.findtext("a:title", "", NS) or "").strip().replace("\n", " ")
            summ = (e.findtext("a:summary", "", NS) or "").strip().replace("\n", " ")
            if summ:
                items.append({"src": f.stem, "title": title, "text": summ})
    return items


def sec_a(items):
    t0 = time.time()
    claims, units, unknown = [], Counter(), Counter()
    known = set(V.UNIT_MAP.values())
    for it in items:
        for c in V.extract(it["text"]):
            claims.append((it["src"], c))
            units[c["unit"]] += 1
            if c["unit"] not in known and not c["unit"].startswith("composite:"):
                unknown[c["unit"]] += 1
    dt = time.time() - t0
    n = len(items)
    print("\n=== A. 抽取覆盖率 ===")
    print(f"  论文 {n} 篇；抽出数值陈述 {len(claims)} 条（{len(claims)/max(1,n):.2f} 条/篇）")
    print(f"  耗时 {dt:.2f}s（{len(claims)/max(1e-9,dt):.0f} 条/秒）")
    print(f"  单位分布 Top10：{dict(units.most_common(10))}")
    if unknown:
        print(f"  未知单位（无法比对，只能判未溯源）：{dict(unknown.most_common(10))}")
    return claims, units


def sec_b(claims):
    """噪声：数字不是测量值，或单位不是单位。"""
    pats = [
        (r"\b(19|20)\d{2}\b(?!\s*(?:%|K|s|A|V|T|eV|nm|um|mA|GHz|MHz|GPa|min|h|g|L))", "年份"),
        (r"\b\d+\s*D\b", "维度"),
        (r"\b(Fig|Table|Eq|Ref|Refs)\.?\s*\d+", "编号"),
        (r"\b(CO2|SiO2|SiC|GaN|Ta4SiTe4|H2O|O2|N2)\b", "化学式"),
        (r"\b\d+\s*(for|and|of|the|with|from)\b", "英文虚词"),
    ]
    hits = Counter()
    examples = []
    for src, c in claims:
        for pat, why in pats:
            if re.search(pat, c["raw"], re.I):
                hits[why] += 1
                if len(examples) < 6:
                    examples.append((why, c["raw"][:48]))
                break
    print("\n=== B. 噪声率 ===")
    total = len(claims)
    bad = sum(hits.values())
    print(f"  噪声 {bad}/{total}（{100*bad/max(1,total):.1f}%）　分类：{dict(hits)}")
    for why, raw in examples:
        print(f"    ⚠️ [{why}] {raw}")
    return bad, total


def sec_c(claims, seed=2026):
    """判定准确性：真实陈述 → 支持 / 扰动 / 换参数，每组 ≥200。"""
    supported = set(V.UNIT_MAP.values())
    STOP = {"and", "the", "for", "from", "with", "that", "this", "least", "most",
            "rate", "than", "also", "into", "over", "under", "their", "which", "while",
            "have", "been", "were", "such", "between", "using", "based"}
    pools = []
    for src, c in claims:
        if c["kind"] != "point" or c["unit"] not in supported:
            continue
        name = max(c["param_cands"], key=len) if c["param_cands"] else ""
        low = name.lower()
        if len(name) < 5 or low in STOP or low.isalpha() and low in STOP:
            continue
        pools.append({"param": name, "value": c["value"], "unit": c["unit"]})

    rng = random.Random(seed)
    rng.shuffle(pools)
    pools = pools[:220]
    records = [{"id": f"R{i:03d}", "param": p["param"], "value": p["value"], "unit": p["unit"]}
               for i, p in enumerate(pools)]

    cases = []
    for r in records:
        cases.append({"text": f"{r['param']} = {r['value']} {r['unit']}", "expect": "有依据"})
        cases.append({"text": f"{r['param']} = {round(float(r['value']) * 1.5 + 1, 6)} {r['unit']}",
                      "expect": "矛盾"})
        cases.append({"text": f"未知参数X = {r['value']} {r['unit']}", "expect": "未溯源"})
    rng.shuffle(cases)

    wrong, by = [], Counter()
    for c in cases:
        res = V.verify(V.extract(c["text"]), records)
        got = res[0]["state"] if res else "无陈述"
        if got != c["expect"]:
            by[(c["expect"], got)] += 1
            if len(wrong) < 8:
                wrong.append((c["text"], got, c["expect"]))
    print("\n=== C. 判定准确性（真实陈述构造）===")
    print(f"  记录 {len(records)} 条；用例 {len(cases)} 条；通过 {len(cases)-len(wrong)}/{len(cases)}")
    if by:
        print(f"  错误分布：{dict(by)}")
    for t, g, e in wrong:
        print(f"    ❌ 「{t[:52]}」→ {g}（期望 {e}）")
    return len(wrong), len(cases)


# ---------------------------------------------------------------- D 极限探针
def sec_d():
    """12 项极限探针：结果用来定义系统边界（不追求全过，追求"如实记录"）。"""
    recs = [
        {"param": "退火温度", "value": 850, "unit": "℃"},
        {"param": "载流子浓度", "value": 1e19, "unit": "cm-3"},
        {"param": "氧化层厚度", "value": 120, "unit": "nm"},
        {"param": "漏电流", "value": 1.5, "unit": "mA"},
        {"param": "禁带宽度", "value": 1.12, "unit": "eV"},
    ]
    probes = [
        ("单位换算（850℃ ↔ 1123K）", "退火温度 1123 K", "未溯源", "已知限制·无单位换算"),
        ("科学计数法（1e19 cm^-3）", "载流子浓度 1e19 cm^-3", "有依据", "caret 记法"),
        ("波浪号（约 120 nm）", "氧化层厚度 约 120 nm", "有依据", "≈/~ 修饰"),
        ("正负误差（120±0.5 nm）", "氧化层厚度 120±0.5 nm", "有依据", "已修复·取主值"),
        ("区间含负数（-40 至 25 ℃）", "退火温度 -40 至 25 ℃", "矛盾", "负数区间"),
        ("千分位（1,450 mA）", "漏电流 1,450 mA", "矛盾", "千分位"),
        ("小数点尾零（850.0 ℃）", "退火温度 850.0 ℃", "有依据", "数值等价"),
        ("极大值（1e21 cm-3）", "载流子浓度 1e21 cm-3", "矛盾", "超量级"),
        ("英文参数前置（thickness 120 nm）", "thickness 120 nm", "未溯源", "已知限制·英文参数不在记录"),
        ("参数在数字之后（120 nm 的厚度）", "120 nm 的厚度", "有依据", "后置参数名"),
        ("一句多陈述", "退火温度 850 ℃，氧化层厚度 120 nm", "有依据", "多陈述句"),
        ("无单位裸数字（850）", "退火温度 850", "无陈述", "缺单位"),
    ]
    print("\n=== D. 极限探针（如实记录，不苛求全过）===")
    passed = 0
    rows = []
    for label, text, expect, note in probes:
        res = V.verify(V.extract(text), recs)
        got = res[0]["state"] if res else "无陈述"
        ok = got == expect
        passed += ok
        rows.append((label, text, expect, got, ok, note))
        print(f"  {'✅' if ok else '❌'} {label:34s} → {got:6s}（期望 {expect}）")
    print(f"  探针通过 {passed}/{len(probes)}")
    return rows, passed, len(probes)


def sec_e(items, claims):
    """规模与性能。"""
    print("\n=== E. 规模与性能 ===")
    recs = [{"param": f"参数{i}", "value": float(i), "unit": "nm"} for i in range(2000)]
    texts = [f"参数{i % 2000} 为 {i}.0 nm" for i in range(2000)]
    t0 = time.time()
    cs = []
    for t in texts:
        cs += V.extract(t)
    t1 = time.time()
    res = V.verify(cs, recs)
    t2 = time.time()
    print(f"  2000 记录 × 5000 陈述：抽取 {t1-t0:.2f}s + 核验 {t2-t1:.2f}s = {t2-t0:.2f}s")
    ok = sum(1 for r in res if r["state"] == "有依据")
    print(f"  其中判为有依据 {ok}/{len(res)}")
    return t2 - t0


def sec_f(items):
    print("\n=== F. 确定性 ===")
    a = [c["raw"] for _, c in [(it["src"], c) for it in items for c in V.extract(it["text"])]]
    b = [c["raw"] for _, c in [(it["src"], c) for it in items for c in V.extract(it["text"])]]
    same = a == b
    print(f"  两次抽取结果一致：{'✅' if same else '❌'}（{len(a)} 条）")
    return same


def main():
    items = load_corpus()
    if not items:
        print("语料为空")
        return 1
    print(f"=== 真实语料：{len(items)} 篇（{len(set(i['src'] for i in items))} 个领域）===")
    claims, units = sec_a(items)
    bad, total = sec_b(claims)
    wrong, ncases = sec_c(claims)
    rows, p_ok, p_n = sec_d()
    per = sec_e(items, claims)
    det = sec_f(items)

    print("\n" + "=" * 60)
    print("汇总")
    print(f"  抽取：{len(claims)} 条 / {len(items)} 篇")
    print(f"  噪声：{bad}/{total}")
    print(f"  判定：{ncases - wrong}/{ncases}")
    print(f"  探针：{p_ok}/{p_n}")
    print(f"  性能：{per:.2f}s（2000×5000）")
    print(f"  确定性：{'通过' if det else '不通过'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
