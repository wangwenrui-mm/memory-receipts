"""新旧论文冲突（数据被后续论文推翻）测试。

场景：
  论文 A（2024）：某材料退火温度 850 ℃
  论文 B（2026）：修正为 900 ℃

期望：
  陈述 900 ℃  → 有依据（新值）
  陈述 850 ℃  → 已过时（旧值，被 B 推翻）
  陈述 950 ℃  → 矛盾
  无日期字段时 → 保持旧行为（任一命中即有依据）
  不同样本（scope 不同）→ 互不覆盖，两个都算有效
"""
# --- 输出编码保护（Windows 控制台默认 GBK，会让 emoji/中文崩溃）---
try:
    import sys as _sys
    for _s in (_sys.stdout, _sys.stderr):
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lab.src import verify as V  # noqa: E402

CASES = []


def add(name, records, text, expect):
    CASES.append({"name": name, "records": records, "text": text, "expect": expect})


OLD = {"id": "A-1", "param": "退火温度", "value": 850, "unit": "℃",
       "date": "2024-03-01", "source": "PaperA", "scope": "样品1"}
NEW = {"id": "B-1", "param": "退火温度", "value": 900, "unit": "℃",
       "date": "2026-05-01", "source": "PaperB", "scope": "样品1"}

add("新值可溯源", [OLD, NEW], "退火温度 900 ℃", "有依据")
add("旧值应判已过时", [OLD, NEW], "退火温度 850 ℃", "已过时")
add("都不是则矛盾", [OLD, NEW], "退火温度 950 ℃", "矛盾")
add("只有旧记录时用旧值", [OLD], "退火温度 850 ℃", "有依据")
add("只有旧记录时说新值", [OLD], "退火温度 900 ℃", "矛盾")
add("无日期字段=旧行为", [{"param": "退火温度", "value": 850, "unit": "℃"}],
    "退火温度 850 ℃", "有依据")
add("未声明样本不作废旧值", [dict(OLD, scope=""), dict(NEW, scope="")],
    "退火温度 850 ℃", "有依据")
add("显式 superseded_by 判过时",
    [dict(OLD, superseded_by="B-1"), dict(NEW)],
    "退火温度 850 ℃", "已过时")
add("同参数不同样本互不覆盖",
    [dict(OLD, scope="样品1"), dict(NEW, scope="样品2")],
    "退火温度 850 ℃", "有依据")
add("同参数同样本才覆盖",
    [dict(OLD, scope="样品1"), dict(NEW, scope="样品1")],
    "退火温度 850 ℃", "已过时")
add("新值范围覆盖", [OLD, NEW], "退火温度 880–920 ℃", "有依据")
add("旧值范围已过时", [OLD, NEW], "退火温度 840–860 ℃", "已过时")
add("三版本取最新", [dict(OLD, date="2023-01-01", value=800, scope="样品1"),
                     dict(OLD, date="2024-03-01", value=850, scope="样品1"),
                     dict(NEW, date="2026-05-01", value=900, scope="样品1")],
    "退火温度 800 ℃", "已过时")


def main():
    ok = 0
    print(f"=== 新旧论文冲突测试：{len(CASES)} 例 ===")
    for c in CASES:
        res = V.verify(V.extract(c["text"]), c["records"])
        got = res[0]["state"] if res else "无陈述"
        good = got == c["expect"]
        ok += good
        print(f"  {'✅' if good else '❌'} {c['name']:22s} 「{c['text']}」→ {got}"
              + ("" if good else f"（期望 {c['expect']}）")
              + (f"  | {res[0]['why']}" if res and not good else ""))
    print(f"通过 {ok}/{len(CASES)}")
    return 0 if ok == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
