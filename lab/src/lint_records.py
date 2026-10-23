"""记录入库检查（lint）：按《参数命名规范》校验 records.jsonl。

用法：
    python lint_records.py [records.jsonl]

检查项：
  1. 必填字段（id / param / value / unit / date / source）
  2. 单位是否在受控词表内
  3. 参数名是否可疑（含数字、含英文、过短、含空格）
  4. 同参数多值是否声明了 scope 或 superseded_by（否则视为同一量的多版本）
  5. 数值是否被写成了带单位的字符串
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
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE.parent))
from lab.src import verify as V  # noqa: E402

REQUIRED = ("id", "param", "value", "unit", "date", "source")
KNOWN_UNITS = set(V.UNIT_MAP.keys())


def lint(path: Path) -> int:
    if not path.exists():
        print(f"文件不存在：{path}")
        return 2

    rows, bad_json = [], 0
    for i, ln in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except Exception:
            bad_json += 1
            print(f"  ❌ 第 {i} 行不是合法 JSON")

    errors = {"missing": [], "unit": [], "param": [], "value": [], "conflict": []}

    for r in rows:
        rid = r.get("id") or f"<无id:{r.get('param')}>"
        for f in REQUIRED:
            if not r.get(f) and r.get(f) != 0:
                errors["missing"].append(f"{rid} 缺字段 {f}")
        u = str(r.get("unit") or "")
        if u and u not in KNOWN_UNITS:
            errors["unit"].append(f"{rid} 单位不在词表：{u}")
        p = str(r.get("param") or "")
        if p:
            if any(ch.isdigit() for ch in p):
                errors["param"].append(f"{rid} 参数名含数字：{p}")
            if len(p) < 2:
                errors["param"].append(f"{rid} 参数名过短：{p}")
            if " " in p:
                errors["param"].append(f"{rid} 参数名含空格：{p}")
            if p.isascii() and p.replace("_", "").isalpha() is False:
                errors["param"].append(f"{rid} 参数名疑似非法字符：{p}")
        v = r.get("value")
        if isinstance(v, str) and any(c.isalpha() or c in "℃%" for c in v):
            errors["value"].append(f"{rid} value 应为纯数字，却写成了：{v}")

    # 同参数多值：必须有 scope 或 superseded_by
    by_param = {}
    for r in rows:
        by_param.setdefault(str(r.get("param") or ""), []).append(r)
    for p, grp in by_param.items():
        vals = {r.get("value") for r in grp}
        if len(vals) > 1:
            unscoped = [r for r in grp if not r.get("scope") and not r.get("superseded_by")]
            if len(unscoped) > 1:
                errors["conflict"].append(
                    f"参数「{p}」有 {len(vals)} 个不同取值，且 {len(unscoped)} 条未声明 scope"
                    f" → 无法判断哪个是当前值")

    total = sum(len(v) for v in errors.values())
    print(f"=== 记录检查：{path.name} ===")
    print(f"  记录 {len(rows)} 条；问题 {total} 项"
          + (f"；JSON 错误 {bad_json} 行" if bad_json else ""))
    titles = {"missing": "缺少必填字段", "unit": "单位不在词表", "param": "参数名可疑",
              "value": "数值格式问题", "conflict": "同参数冲突（未声明 scope）"}
    for k, items in errors.items():
        if items:
            print(f"  [{titles[k]}] {len(items)} 项")
            for x in items[:5]:
                print(f"    - {x}")
    if total == 0:
        print("  ✅ 全部通过")
    return 0 if total == 0 and bad_json == 0 else 1


def main():
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "data" / "records.jsonl"
    if not p.is_absolute():
        p = BASE / p
    return lint(p)


if __name__ == "__main__":
    raise SystemExit(main())
