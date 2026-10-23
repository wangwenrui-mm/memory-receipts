"""出厂收关：50 组独立盲测（全新种子，不复用验收集）。

设计：
- 50 条基准记录 + 50 条陈述，分四类（有依据/矛盾/未溯源/计划）
- 种子 2026，与 gen_acceptance.py 的 42/7 不同 → 独立样本
- 顺便验证「确定性」：同一输入跑两遍，判定必须完全一致
- 任一失败 → 退出码 1（可接 CI）

用法：
    python final_smoke50.py
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
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lab.src import verify as V  # noqa: E402

SEED = 2026

PARAMS = [
    ("退火温度", "℃", (200, 1100)),
    ("退火时间", "min", (5, 120)),
    ("氧化层厚度", "nm", (5, 300)),
    ("载流子迁移率", "cm²", (50, 3000)),
    ("载流子浓度", "cm-2", (1e15, 1e20)),
    ("栅极电压", "V", (0.5, 12)),
    ("漏电流", "mA", (0.01, 100)),
    ("阈值电压", "V", (0.2, 3)),
    ("表面粗糙度", "nm", (0.1, 10)),
    ("晶粒尺寸", "um", (0.05, 5)),
]


def build():
    rng = random.Random(SEED)
    records = []
    for i in range(50):
        name, unit, (lo, hi) = PARAMS[i % len(PARAMS)]
        v = rng.uniform(lo, hi)
        v = f"{v:.3e}" if unit == "cm-2" and v > 1e6 else round(v, 3)
        records.append({"id": f"S-{i+1:02d}", "param": name, "value": v, "unit": unit})

    by = {}
    for r in records:
        by.setdefault(r["param"], []).append(r)

    def one(p):
        return rng.choice(by[p])

    cases = []

    # 有依据 18
    for _ in range(6):
        r = one("退火温度")
        cases.append((f"退火温度为 {r['value']} ℃", "有依据"))
    for _ in range(6):
        r = one("氧化层厚度")
        cases.append((f"氧化层厚度 {r['value']} nm", "有依据"))
    for _ in range(6):
        r = one(rng.choice(list(by)))
        cases.append((f"{r['param']}为 {r['value']} {r['unit']}", "有依据"))

    # 矛盾 15
    for _ in range(15):
        r = one(rng.choice(list(by)))
        try:
            wrong = round(float(r["value"]) * 1.9 + 3, 3)
        except Exception:
            wrong = 12345.0
        cases.append((f"{r['param']}为 {wrong} {r['unit']}", "矛盾"))

    # 未溯源 10
    for t in ["量子阱宽度为 8 nm", "钝化温度 400 ℃", "热导率 150 W/mK",
              "腐蚀速率 12 nm/min", "界面态密度 3e11 eV-1cm-2",
              "光致发光峰 450 nm", "衬底偏压 -30 V", "退火气压 1.2 Torr",
              "氧化层厚度 120 μm", "退火温度 850 分钟"]:
        cases.append((t, "未溯源"))

    # 计划 7
    for t in ["后续把退火温度提高到 900 ℃", "计划退火时间延长到 90 min",
              "下一步将栅极电压调到 8 V", "拟把氧化层厚度减到 80 nm",
              "准备做 3e19 cm-2 的掺杂", "预计漏电流控制在 0.5 mA",
              "目标值：迁移率 2000 cm²"]:
        cases.append((t, "计划"))

    rng.shuffle(cases)
    return records, [{"text": t, "expect": e} for t, e in cases]


def run(records, cases):
    out = []
    for c in cases:
        res = V.verify(V.extract(c["text"]), records)
        out.append(res[0]["state"] if res else "无陈述")
    return out


def main():
    records, cases = build()
    print(f"=== 出厂收关盲测（种子 {SEED}）===")
    print(f"基准记录 {len(records)} 条；陈述 {len(cases)} 条\n")

    t0 = time.time()
    got = run(records, cases)
    t1 = time.time()

    wrong = []
    for c, g in zip(cases, got):
        if g != c["expect"]:
            wrong.append((c["text"], g, c["expect"]))

    for i, (c, g) in enumerate(zip(cases, got), 1):
        mark = "✅" if g == c["expect"] else "❌"
        print(f"  {i:2d}. {mark} [{g}] 期望[{c['expect']}] {c['text'][:38]}")

    print(f"\n通过 {len(cases) - len(wrong)}/{len(cases)}　耗时 {t1 - t0:.3f}s")
    if wrong:
        print("\n失败项：")
        for t, g, e in wrong:
            print(f"  ❌ 「{t}」→ {g}（期望 {e}）")

    # 确定性校验：再跑一遍，必须完全一致
    got2 = run(records, cases)
    same = got == got2
    print(f"确定性（两次结果一致）：{'✅' if same else '❌'}")
    if not same:
        for i, (a, b) in enumerate(zip(got, got2)):
            if a != b:
                print(f"  不一致 #{i + 1}：{a} vs {b}")

    return 0 if (not wrong and same) else 1


if __name__ == "__main__":
    raise SystemExit(main())
