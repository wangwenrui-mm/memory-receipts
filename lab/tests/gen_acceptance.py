"""生成验收测试集：200 条基准记录 + 60 条带标注陈述。

确定性生成（固定随机种子），每次产出的文件一致，可复现。
数据全部为虚构的半导体/器件工艺参数，不涉及任何真实实验。
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
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]          # lab/
OUT = BASE / "tests" / "acceptance_cases.json"

PARAMS = [
    # (参数名, 别名, 单位, 值域)
    ("退火温度", "温度", "℃", (200, 1100)),
    ("退火时间", "时间", "min", (5, 120)),
    ("氧化层厚度", "厚度", "nm", (5, 300)),
    ("薄膜厚度", "厚度", "nm", (50, 800)),
    ("载流子迁移率", "迁移率", "cm²", (50, 3000)),
    ("载流子浓度", "浓度", "cm-2", (1e15, 1e20)),
    ("栅极电压", "栅压", "V", (0.5, 12)),
    ("漏电流", "电流", "mA", (0.01, 100)),
    ("阈值电压", "阈值", "V", (0.2, 3)),
    ("击穿电压", "击穿", "V", (20, 600)),
    ("表面粗糙度", "粗糙度", "nm", (0.1, 10)),
    ("晶粒尺寸", "晶粒", "um", (0.05, 5)),
    ("掺杂浓度", "掺杂", "cm-2", (1e14, 1e19)),
    ("电阻率", "电阻", "Pa", (1e-6, 1e-2)),
    ("介电常数", "介电", "%", (2, 100)),
    ("禁带宽度", "禁带", "eV", (0.5, 6)),
    ("膜应力", "应力", "Pa", (1e6, 1e9)),
    ("反射率", "反射", "%", (1, 99)),
    ("透过率", "透过", "%", (1, 99)),
    ("接触电阻", "接触", "Pa", (1e-7, 1e-3)),
]


def gen_records(n=200):
    rng = random.Random(42)
    records = []
    for i in range(n):
        name, alias, unit, (lo, hi) = PARAMS[i % len(PARAMS)]
        value = rng.uniform(lo, hi)
        if unit in ("cm-2", "Pa") and value >= 1e6:
            value = f"{value:.3e}"          # 科学计数法存储
        else:
            value = round(value, 3)
        records.append({"id": f"R-{i + 1:03d}",
                        "date": f"2026-{1 + (i % 3):02d}-{1 + (i % 28):02d}",
                        "param": name,
                        "alias": alias,
                        "value": value,
                        "unit": unit,
                        "note": ""})
    return records


def build_cases(records):
    rng = random.Random(7)
    by_name = {}
    for r in records:
        by_name.setdefault(r["param"], []).append(r)

    def sample(pname, k=1):
        pool = by_name.get(pname, [])
        return rng.sample(pool, min(k, len(pool)))

    cases = []

    # ---- 有依据 ×20 ----
    for r in sample("退火温度", 4):
        cases.append((f"退火温度为 {r['value']} ℃", "有依据"))
    for r in sample("氧化层厚度", 4):
        cases.append((f"氧化层厚度测得 {r['value']} nm", "有依据"))
        cases.append((f"样品在 {r['value']} nm 的厚度下测试", "有依据"))
    for r in sample("载流子浓度", 4):
        cases.append((f"载流子浓度为 {r['value']} cm-2", "有依据"))
    for r in sample("栅极电压", 4):
        cases.append((f"栅极电压设为 {r['value']} V", "有依据"))
    # 单位别名 / 参数别名 / 范围
    r = sample("退火温度", 1)[0]
    cases.append((f"退火温度 {r['value']} °C", "有依据"))
    cases.append((f"温度保持在 {r['value']} ℃", "有依据"))
    lo = max(200, r["value"] - 20)
    hi = r["value"] + 20
    cases.append((f"退火温度 {lo}–{hi} ℃", "有依据"))
    r = sample("载流子迁移率", 1)[0]
    cases.append((f"迁移率达到 {r['value']} cm²", "有依据"))

    # ---- 矛盾 ×20 ----
    for _ in range(8):
        r = rng.choice(records)
        cases.append((f"{r['param']}为 {round(rng.uniform(*_shift(r)), 3)} {r['unit']}", "矛盾"))
    r = sample("退火温度", 1)[0]
    cases.append((f"退火温度 100–150 ℃", "矛盾"))
    cases.append((f"温度降到 -5 ℃", "矛盾"))
    r = sample("漏电流", 1)[0]
    cases.append((f"漏电流为 {r['value'] * 2.5} mA", "矛盾"))
    r = sample("阈值电压", 1)[0]
    cases.append((f"阈值电压 {r['value'] + 5} V", "矛盾"))
    for _ in range(8):
        r = rng.choice(records)
        cases.append((f"{r['param']}记录为 {r['value'] + _delta(r)} {r['unit']}", "矛盾"))

    # ---- 未溯源 ×10 ----
    cases.append(("量子阱宽度为 8 nm", "未溯源"))
    cases.append(("表面钝化温度 400 ℃", "未溯源"))
    cases.append(("氧化层厚度 120 μm", "未溯源"))      # 单位不一致
    cases.append(("退火温度 850 分钟", "未溯源"))       # 单位不一致
    cases.append(("热导率 150 W/mK", "未溯源"))         # 复合单位不支持
    cases.append(("界面态密度 3e11 eV-1cm-2", "未溯源"))
    cases.append(("光致发光峰位 450 nm", "未溯源"))
    cases.append(("腐蚀速率 12 nm/min", "未溯源"))
    cases.append(("退火气压 1.2 Torr", "未溯源"))
    cases.append(("衬底偏压 -30 V", "未溯源"))

    # ---- 计划 ×10 ----
    cases.append(("后续把退火温度提高到 900 ℃", "计划"))
    cases.append(("计划退火时间延长到 90 min", "计划"))
    cases.append(("下一步将栅极电压调到 8 V", "计划"))
    cases.append(("拟把氧化层厚度减到 80 nm", "计划"))
    cases.append(("准备做 3e19 cm-2 的掺杂", "计划"))
    cases.append(("预计漏电流控制在 0.5 mA", "计划"))
    cases.append(("目标值：迁移率 2000 cm²", "计划"))
    cases.append(("规划后续测 -40 ℃ 低温特性", "计划"))
    cases.append(("后续 850–950 ℃ 之间扫温", "计划"))
    cases.append(("准备把击穿电压测到 500 V", "计划"))

    return [{"text": t, "expect": e} for t, e in cases]


def _shift(r):
    lo, hi = {"℃": (200, 1100), "min": (5, 120), "nm": (5, 300), "cm²": (50, 3000),
              "cm-2": (1e15, 1e20), "V": (0.5, 12), "mA": (0.01, 100),
              "um": (0.05, 5), "Pa": (1e-6, 1e-2), "%": (2, 100), "eV": (0.5, 6)}[r["unit"]]
    return lo, hi


def _delta(r):
    return r["value"] * 1.7 + 1


def main():
    records = gen_records(200)
    cases = build_cases(records)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"records": records, "cases": cases},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已生成：{len(records)} 条记录、{len(cases)} 条陈述")
    from collections import Counter
    print("标注分布：", dict(Counter(c["expect"] for c in cases)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
