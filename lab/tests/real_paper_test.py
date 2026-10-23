"""真实论文语料测试（arXiv 摘要，cond-mat.mtrl-sci / semiconductor）。

三段测试：
  A. 抽取能力：真实英文摘要里能抽出多少数值型陈述
  B. 噪声控制：不该被当成参数陈述的东西（2D、Fig. 2、2019 年）有没有被抽出来
  C. 判定正确性：用真实陈述构造三组标注（有依据 / 矛盾 / 未溯源），看判定是否准确

用法：
    python real_paper_test.py
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
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lab.src import verify as V  # noqa: E402

BASE = Path(__file__).resolve().parents[1]          # lab/
XML = BASE / "data" / "real_abstracts.xml"
NS = {"a": "http://www.w3.org/2005/Atom"}


def load_abstracts() -> list[dict]:
    tree = ET.parse(XML)
    out = []
    for e in tree.getroot().findall("a:entry", NS):
        title = (e.findtext("a:title", "", NS) or "").strip().replace("\n", " ")
        summ = (e.findtext("a:summary", "", NS) or "").strip().replace("\n", " ")
        out.append({"title": title, "text": summ})
    return out


def supported_units():
    return set(V.UNIT_MAP.values()) | {"composite:"}


def test_a_extraction(abs_list):
    total_claims = 0
    per_abstract = []
    units_seen = {}
    for a in abs_list:
        cl = V.extract(a["text"])
        total_claims += len(cl)
        per_abstract.append(len(cl))
        for c in cl:
            units_seen[c["unit"]] = units_seen.get(c["unit"], 0) + 1
    print("=== A. 抽取能力（真实英文摘要）===")
    print(f"  摘要 {len(abs_list)} 篇，抽出 {total_claims} 条数值陈述"
          f"（平均 {total_claims / max(1, len(abs_list)):.1f} 条/篇）")
    print(f"  覆盖到的单位：{dict(sorted(units_seen.items(), key=lambda x: -x[1])[:10])}")
    return total_claims, units_seen


def test_b_noise(abs_list):
    """不该被抽成"参数陈述"的模式：2D/3D、Fig. N、年份、编号。"""
    bad_patterns = [
        (r"\b\d+\s*D\b", "维度标记(2D/3D)"),
        (r"\bFig\.?\s*\d+", "图号"),
        (r"\b(19|20)\d{2}\b", "年份"),
        (r"\bref\.?\s*\d+", "参考文献号"),
    ]
    noise = []
    for a in abs_list:
        for c in V.extract(a["text"]):
            for pat, why in bad_patterns:
                if re.search(pat, c["raw"]):
                    noise.append((why, c["raw"], c["unit"]))
    print("\n=== B. 噪声控制 ===")
    print(f"  疑似噪声抽取：{len(noise)} 条")
    for why, raw, unit in noise[:8]:
        print(f"    ⚠️ [{why}] {raw[:44]} (unit={unit})")
    return noise


def test_c_verdicts(abs_list):
    """用真实陈述构造三组标注，检验判定正确性。"""
    records, cases = [], []
    supported = supported_units()
    rng_seed = 11
    import random
    rng = random.Random(rng_seed)

    for a in abs_list:
        for c in V.extract(a["text"]):
            if c["unit"] not in supported:
                continue
            if c["kind"] != "point":
                continue
            name = max(c["param_cands"], key=len) if c["param_cands"] else ""
            if len(name) < 3:
                continue
            records.append({"id": f"P-{len(records)+1:03d}", "param": name,
                            "value": c["value"], "unit": c["unit"]})

    # 每条记录造三组
    for r in records[:40]:
        disp = f"{r['value']} {r['unit']}"
        cases.append({"text": f"{r['param']} = {disp}", "expect": "有依据", "src": "real"})
        cases.append({"text": f"{r['param']} = {round(r['value'] * 1.5 + 1, 3)} {r['unit']}",
                      "expect": "矛盾", "src": "perturbed"})
        cases.append({"text": f"量子阱宽度 = {disp}", "expect": "未溯源", "src": "swapped"})

    rng.shuffle(cases)
    wrong = []
    for c in cases:
        res = V.verify(V.extract(c["text"]), records)
        got = res[0]["state"] if res else "无陈述"
        if got != c["expect"]:
            wrong.append((c["text"], got, c["expect"]))
    print("\n=== C. 判定正确性（基于真实陈述）===")
    print(f"  记录 {len(records)} 条；用例 {len(cases)} 条；通过 {len(cases)-len(wrong)}/{len(cases)}")
    for t, g, e in wrong[:10]:
        print(f"    ❌ 「{t[:50]}」→ {g}（期望 {e}）")
    return len(wrong)


def main():
    abs_list = load_abstracts()
    if not abs_list:
        print("没有摘要数据")
        return 1
    print(f"=== 真实语料：{len(abs_list)} 篇 arXiv 摘要 ===")
    for a in abs_list[:3]:
        print(f"  · {a['title'][:70]}")
    test_a_extraction(abs_list)
    noise = test_b_noise(abs_list)
    bad = test_c_verdicts(abs_list)
    print("\n=== 结论 ===")
    print(f"  噪声 {len(noise)} 条；判定错误 {bad} 条")
    return 0 if (not noise and not bad) else 1


if __name__ == "__main__":
    raise SystemExit(main())
