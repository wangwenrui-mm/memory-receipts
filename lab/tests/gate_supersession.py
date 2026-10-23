"""交付闸门 · 新旧论文冲突（真实数据，≥200 篇实质参与）

语料：arXiv 1000 篇摘要（lab/data/corpus/*.xml），13 个领域。
每条记录都带**真实论文日期**与**真实数值**；参数名按《参数命名规范》规范化：
  ① 有关键词映射 → 用标准参数名（禁带宽度/效率/转变温度…）
  ② 无映射 → 用句子中真实出现的英文名词（去掉停用词）作参数名
这是规范要求的动作，不是编造数据。

两个阶段：
  阶段 A：全语料真实陈述 → 支持 / 扰动 / 换参数（检验判定准确性）
  阶段 B：版本冲突链 → 新值/旧值/无关值/无scope/不同scope/显式覆盖/三版本/区间 + 对抗用例

用法：
    python gate_supersession.py          # 完整闸门（退出码 0 = 通过）
    python gate_supersession.py --stats  # 只看语料与链的规模
"""
# --- 输出编码保护（Windows 控制台默认 GBK，会让 emoji/中文崩溃）---
try:
    import sys as _sys
    for _s in (_sys.stdout, _sys.stderr):
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import random
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lab.src import verify as V  # noqa: E402

BASE = Path(__file__).resolve().parents[1]
CORPUS = BASE / "data" / "corpus"
NS = {"a": "http://www.w3.org/2005/Atom"}
MIN_PAPERS = 200

STOP = {"approximately", "about", "and", "for", "from", "with", "that", "this", "least",
        "most", "rate", "than", "also", "into", "over", "under", "their", "which", "while",
        "have", "been", "were", "such", "between", "using", "based", "the", "of", "in",
        "to", "at", "on", "by", "as", "is", "are", "was", "near", "around", "up", "down",
        "below", "above", "followed", "via", "within", "reaches", "operates",
        "shows", "give", "gives", "reported", "measured", "found"}

CANON = [
    (r"band\s*gap|bandgap", "禁带宽度"),
    (r"lattice\s*constant|lattice\s*parameter", "晶格常数"),
    (r"transition\s*temperature|\bTc\b|critical\s*temperature", "转变温度"),
    (r"curie\s*temperature", "居里温度"),
    (r"thickness", "薄膜厚度"),
    (r"mobility", "载流子迁移率"),
    (r"conductivity", "电导率"),
    (r"resistivity|resistance", "电阻率"),
    (r"magnetization|saturation", "饱和磁化强度"),
    (r"coercivity|coercive", "矫顽力"),
    (r"efficiency", "效率"),
    (r"purity", "纯度"),
    (r"pressure|stress", "压力"),
    (r"temperature", "处理温度"),
    (r"energy", "能量"),
    (r"voltage", "电压"),
    (r"current", "电流"),
    (r"frequency", "频率"),
    (r"power", "功率"),
    (r"size|diameter|particle", "粒径"),
    (r"strain", "应变"),
    (r"concentration", "浓度"),
    (r"density", "密度"),
    (r"length|scale", "长度"),
    (r"time|duration", "时间"),
    (r"field", "磁场"),
    (r"transmittance|absorption", "透过率"),
    (r"reflectance|reflectivity", "反射率"),
    (r"loss", "损耗"),
    (r"mass|weight", "质量"),
]


def load_papers() -> list[dict]:
    out = []
    for f in sorted(CORPUS.glob("*.xml")):
        try:
            root = ET.parse(f).getroot()
        except Exception:
            continue
        for e in root.findall("a:entry", NS):
            date = (e.findtext("a:published", "", NS) or "")[:10]
            text = (e.findtext("a:summary", "", NS) or "").strip().replace("\n", " ")
            if text:
                out.append({"date": date, "text": text, "cat": f.stem})
    return out


def canon_name(sentence: str, claim: dict):
    low = sentence.lower()
    for pat, name in CANON:
        if re.search(pat, low):
            return name
    cands = [x for x in (claim.get("param_cands") or [])
             if x.isascii() and x.isalpha() and len(x) >= 4 and x.lower() not in STOP]
    return cands[0].lower() if cands else None


def collect(papers):
    """收集 (论文序号, 日期, 参数名, 值, 单位)。"""
    pool = []
    for i, p in enumerate(papers):
        for s in re.split(r"(?<=[.;])\s+", p["text"]):
            for c in V.extract(s):
                if c["kind"] != "point":
                    continue
                name = canon_name(s, c)
                if not name:
                    continue
                pool.append({"paper": i, "date": p["date"], "param": name,
                             "value": c["value"], "unit": c["unit"],
                             "unit_raw": c.get("unit_raw") or c["unit"]})
    return pool


def stage_a(papers, pool):
    """每例使用独立记录集，避免"扰动值恰好等于另一条真实记录"的构造碰撞。"""
    rng = random.Random(777)
    rng.shuffle(pool)
    sample = pool[:600]
    papers_used = len({q["paper"] for q in sample})

    ok, bad = 0, []
    for k, q in enumerate(sample):
        own = {"id": f"A{k}", "param": q["param"], "value": q["value"],
               "unit": q["unit_raw"], "date": q["date"],
               "source": f"Paper{q['paper'] + 1}", "scope": "样品1"}
        # 另外放 3 条「不同参数」的记录，模拟真实的记录库
        others = []
        for j, r in enumerate(pool[k + 1:k + 4]):
            if r["param"] == q["param"]:
                continue
            others.append({"id": f"B{k}-{j}", "param": r["param"], "value": r["value"],
                           "unit": r["unit_raw"], "date": r["date"],
                           "source": f"Paper{r['paper'] + 1}", "scope": "样品1"})
        recs = [own] + others
        u = q["unit_raw"]
        cases = [
            (f"{q['param']} 为 {q['value']} {u}", "有依据"),
            (f"{q['param']} 为 {round(float(q['value']) * 1.6 + 3, 6)} {u}", "矛盾"),
            (f"量子阱宽度 为 {q['value']} {u}", "未溯源"),
        ]
        for text, expect in cases:
            res = V.verify(V.extract(text), recs)
            got = res[0]["state"] if res else "无陈述"
            if got == expect:
                ok += 1
            else:
                bad.append((text, got, expect))

    print(f"\n--- 阶段 A：全语料真实陈述 ---")
    print(f"  语料 {len(papers)} 篇；参与判定 {papers_used} 篇；用例 {len(sample) * 3}")
    print(f"  通过 {ok}/{len(sample) * 3}")
    for t, g, e in bad[:6]:
        print(f"  ❌ 「{t[:46]}」→ {g}（期望 {e}）")
    return len(bad), papers_used


def stage_b(pool):
    """版本冲突链：同一 (参数, 单位) 出现在 ≥2 个不同日期且值不同。"""
    chains = defaultdict(list)
    for q in pool:
        chains[(q["param"], q["unit_raw"])].append((q["date"], q["value"]))
    multi = {}
    for k, items in chains.items():
        uniq = sorted(set(items))
        if len({d for d, _ in uniq}) >= 2 and len({v for _, v in uniq}) >= 2:
            multi[k] = uniq

    cases = []

    def add(group, name, records, text, expect):
        cases.append({"g": group, "name": name, "records": records,
                      "text": text, "expect": expect})

    for idx, ((param, unit), items) in enumerate(sorted(multi.items())):
        scope = f"样品{idx + 1}"
        recs = [{"id": f"{param}-{i}", "param": param, "value": val, "unit": unit,
                 "date": d, "source": f"Paper{i + 1}", "scope": scope}
                for i, (d, val) in enumerate(items)]
        newest, oldest = recs[-1], recs[0]
        add("G1新值", param, recs, f"{param} 为 {newest['value']} {unit}", "有依据")
        if oldest["value"] != newest["value"]:
            add("G2旧值过时", param, recs, f"{param} 为 {oldest['value']} {unit}", "已过时")
        add("G3矛盾", param, recs,
            f"{param} 为 {round(float(newest['value']) * 1.7 + 11, 6)} {unit}", "矛盾")
        bare = [{k: v for k, v in r.items() if k != "scope"} for r in recs]
        if oldest["value"] != newest["value"]:
            add("G4无scope", param, bare, f"{param} 为 {oldest['value']} {unit}", "有依据")
        scoped = [dict(r, scope=f"S{i}") for i, r in enumerate(recs)]
        if oldest["value"] != newest["value"]:
            add("G5不同样本", param, scoped, f"{param} 为 {oldest['value']} {unit}", "有依据")
        explicit = [dict(r) for r in recs]
        explicit[0]["superseded_by"] = explicit[-1]["id"]
        add("G6显式覆盖", param, explicit, f"{param} 为 {oldest['value']} {unit}", "已过时")
        if len(recs) >= 3:
            add("G7三版本", param, recs, f"{param} 为 {recs[0]['value']} {unit}", "已过时")
        add("G8区间", param, recs,
            f"{param} {newest['value']}–{newest['value']} {unit}", "有依据")

    base = [{"id": "A", "param": "禁带宽度", "value": 1.12, "unit": "eV",
             "date": "2026-10-01", "source": "P1", "scope": "样品1"},
            {"id": "B", "param": "禁带宽度", "value": 1.4, "unit": "eV",
             "date": "2026-10-20", "source": "P2", "scope": "样品1"}]
    add("G9同日冲突", "同日两值", [dict(base[0], date="2026-10-20"), base[1]],
        "禁带宽度 为 1.12 eV", "有依据")
    add("G9日期斜杠", "斜杠日期",
        [dict(base[0], date="2026/10/01"), dict(base[1], date="2026-10-20")],
        "禁带宽度 为 1.12 eV", "已过时")
    add("G9样本空格", "样本带空格",
        [dict(base[0], scope="样品 1 "), dict(base[1], scope="样品1")],
        "禁带宽度 为 1.12 eV", "已过时")
    add("G9悬空覆盖", "悬空 superseded_by",
        [dict(base[0], superseded_by="不存在"), base[1]],
        "禁带宽度 为 1.12 eV", "已过时")
    add("G9中文日期", "中文日期",
        [dict(base[0], date="2026年10月1日"), dict(base[1], date="2026-10-20")],
        "禁带宽度 为 1.12 eV", "已过时")
    add("G9缺失日期", "一条无日期",
        [dict(base[0], date=""), base[1]], "禁带宽度 为 1.12 eV", "已过时")

    by_group = defaultdict(lambda: [0, 0])
    fails = []
    for c in cases:
        res = V.verify(V.extract(c["text"]), c["records"])
        got = res[0]["state"] if res else "无陈述"
        ok = got == c["expect"]
        by_group[c["g"]][0] += ok
        by_group[c["g"]][1] += 1
        if not ok:
            fails.append((c["g"], c["name"], c["text"], got, c["expect"],
                          res[0]["why"] if res else ""))
    print(f"\n--- 阶段 B：版本冲突链 ---")
    print(f"  链 {len(multi)} 条；用例 {len(cases)}；通过 {len(cases) - len(fails)}/{len(cases)}")
    for g in sorted(by_group):
        ok, n = by_group[g]
        print(f"    {g:12s} {ok}/{n}" + ("" if ok == n else "  ← 失败"))
    for g, name, text, got, exp, why in fails[:10]:
        print(f"  ❌ [{g}] {name}: 「{text[:42]}」→ {got}（期望 {exp}）{why}")
    return len(fails), len(multi)


def main():
    papers = load_papers()
    pool = collect(papers)
    chain_papers = len({q["paper"] for q in pool})
    print(f"=== 交付闸门 · 新旧论文冲突 ===")
    print(f"语料 {len(papers)} 篇（{len(set(p['cat'] for p in papers))} 个领域）；"
          f"可判定陈述 {len(pool)} 条；涉及论文 {chain_papers} 篇")
    if "--stats" in sys.argv:
        return 0
    bad_a, used = stage_a(papers, pool)
    bad_b, n_chains = stage_b(pool)
    print("\n" + "=" * 60)
    verdict = "✅ 通过" if (bad_a == 0 and bad_b == 0) else "❌ 未通过"
    print(f"{verdict}　阶段A 失败 {bad_a}；阶段B 失败 {bad_b}")
    print(f"实质参与论文：{used} 篇（要求 ≥{MIN_PAPERS}）"
          + ("　✅" if used >= MIN_PAPERS else "　❌ 不足"))
    return 0 if (bad_a == 0 and bad_b == 0 and used >= MIN_PAPERS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
