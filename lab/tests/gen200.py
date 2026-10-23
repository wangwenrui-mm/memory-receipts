"""200 组盲测生成器（每组 = 1 条基准记录 + 对应陈述；另含跨参数用例）。

用法：
    python gen200.py <seed>          # 生成并直接跑，打印失败项
    python gen200.py <seed> --save   # 同时存盘到 tests/round_<seed>.json

设计原则：只测「规格内」的行为，避免把不支持的特性当成 bug。
规格：
  1) 参数绑定：精确名 > 强包含（≥3 字）；限定词不同则视为不同参数
  2) 数值：单位归一后精确相等 → 有依据；不等 → 矛盾
  3) 范围：记录值落在区间内 → 有依据
  4) 单位不一致 / 参数不存在 / 复合单位 → 未溯源
  5) 含计划语 → 计划（不核验）
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
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lab.src import verify as V  # noqa: E402

PARAMS = [
    ("退火温度", "℃", (200, 1100)),
    ("退火时间", "min", (5, 120)),
    ("氧化层厚度", "nm", (5, 300)),
    ("薄膜厚度", "nm", (50, 800)),
    ("载流子迁移率", "cm²", (50, 3000)),
    ("载流子浓度", "cm-2", (1e15, 1e20)),
    ("栅极电压", "V", (0.5, 12)),
    ("漏电流", "mA", (0.01, 100)),
    ("阈值电压", "V", (0.2, 3)),
    ("击穿电压", "V", (20, 600)),
    ("表面粗糙度", "nm", (0.1, 10)),
    ("晶粒尺寸", "um", (0.05, 5)),
    ("掺杂浓度", "cm-2", (1e14, 1e19)),
    ("介电常数", "%", (2, 100)),
    ("禁带宽度", "eV", (0.5, 6)),
    ("反射率", "%", (1, 99)),
    ("透过率", "%", (1, 99)),
    ("退火气压", "Torr", (1e-3, 10)),
    ("膜应力", "Pa", (1e6, 1e9)),
    ("接触电阻", "Pa", (1e-7, 1e-3)),
]

UNIT_ALIAS = {"℃": ["℃", "°C", "度"], "um": ["um", "μm"], "cm²": ["cm²", "cm2", "cm-2"]}


def fmt(v, unit):
    """按单位选择展示格式（科学计数法用于极大/极小值）。"""
    if unit in ("cm-2", "Pa") and (v >= 1e6 or v < 1e-3):
        return f"{v:.3e}"
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return f"{round(v, 3)}"


def build(seed):
    rng = random.Random(seed)
    records, cases = [], []

    # 200 组：每组一条记录 + 一条陈述
    for i in range(200):
        name, unit, (lo, hi) = PARAMS[i % len(PARAMS)]
        v = rng.uniform(lo, hi)
        shown = fmt(v, unit)                     # 陈述里会怎么显示
        v_exact = float(shown)                   # 记录里存同一精度，保证可对齐
        records.append({"id": f"T-{i+1:03d}", "param": name,
                        "value": v_exact, "unit": unit})

        kind = i % 5          # 0-2 有依据（3 种句式），3 矛盾，4 混合
        disp_u = rng.choice(UNIT_ALIAS.get(unit, [unit]))
        s_val = fmt(v_exact, unit)

        if kind in (0, 1, 2):
            style = rng.randrange(5)
            if style == 0:
                text = f"第{i+1}批样品的{name}为 {s_val} {disp_u}"
            elif style == 1:
                text = f"测得 {name} = {s_val}{disp_u}"          # 等号 + 无空格
            elif style == 2:
                lo2 = v_exact * 0.9
                hi2 = v_exact * 1.1
                text = f"{name} 在 {fmt(lo2, unit)}–{fmt(hi2, unit)} {disp_u} 之间"
            elif style == 3:
                text = f"{name}记录为 {s_val} {disp_u}（第{i+1}批）"
            else:
                text = f"本批次{name}：{s_val} {disp_u}"
            cases.append({"text": text, "expect": "有依据"})
        elif kind == 3:
            wrong = v_exact * 1.9 + 3
            cases.append({"text": f"{name}为 {fmt(wrong, unit)} {disp_u}", "expect": "矛盾"})
        else:
            # 计划
            cases.append({"text": f"后续把{name}调整到 {s_val} {disp_u}", "expect": "计划"})

    # 额外 20 组：未溯源 / 边界（明确属于规格内）
    extra = [
        ("量子阱宽度为 8 nm", "未溯源"),
        ("钝化温度 400 ℃", "未溯源"),
        ("热导率 150 W/mK", "未溯源"),
        ("腐蚀速率 12 nm/min", "未溯源"),
        ("氧化层厚度 120 μm", "未溯源"),
        ("退火温度 850 分钟", "未溯源"),
        ("退火气压 1.2 MPa", "未溯源"),
        ("界面态密度 3e11 eV-1cm-2", "未溯源"),
        ("光致发光峰 450 nm", "未溯源"),
        ("衬底偏压 -30 V", "未溯源"),
        ("计划把退火时间延长到 90 min", "计划"),
        ("拟把氧化层厚度减到 80 nm", "计划"),
        ("目标值：迁移率 2000 cm²", "计划"),
        ("预计漏电流控制在 0.5 mA", "计划"),
        ("准备做 3e19 cm-2 的掺杂", "计划"),
        ("下一步将栅极电压调到 8 V", "计划"),
    ]
    for t, e in extra:
        cases.append({"text": t, "expect": e})

    rng.shuffle(cases)
    return records, cases


def run(seed, save=False):
    records, cases = build(seed)
    t0 = time.time()
    got = [V.verify(V.extract(c["text"]), records) for c in cases]
    got = [r[0]["state"] if r else "无陈述" for r in got]
    dt = time.time() - t0

    bad = [(c["text"], g, c["expect"]) for c, g in zip(cases, got) if g != c["expect"]]
    print(f"=== 种子 {seed}：{len(records)} 记录 × {len(cases)} 陈述，耗时 {dt:.2f}s ===")
    print(f"通过 {len(cases) - len(bad)}/{len(cases)}")
    for t, g, e in bad:
        print(f"  ❌ 「{t[:44]}」→ {g}（期望 {e}）")

    if save:
        out = Path(__file__).with_name(f"round_{seed}.json")
        out.write_text(json.dumps({"seed": seed, "records": records, "cases": cases},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        print("已存盘：", out.name)

    return 0 if not bad else 1


if __name__ == "__main__":
    seed = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 5001
    raise SystemExit(run(seed, save="--save" in sys.argv))
