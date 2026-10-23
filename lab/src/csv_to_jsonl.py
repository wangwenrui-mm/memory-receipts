"""把 Excel / CSV 里的实验记录，转成系统要的 JSONL。

为什么需要：手写 JSONL 容易错（少逗号、引号、字段名写错）。
用 Excel 填表 → 导出 CSV → 一条命令转好。

用法：
    python csv_to_jsonl.py 记录表.csv                 # 输出 记录表.jsonl
    python csv_to_jsonl.py 记录表.csv out.jsonl       # 指定输出

CSV 需要表头（中英文都认，顺序随意）：
    id | param/参数 | value/数值 | unit/单位 | date/日期 | source/来源 | scope/样本 | superseded_by/被取代
只有 id/param/value/unit 是必需的，其余可空。

导出 CSV 注意：用 UTF-8 编码（Excel「另存为 → CSV UTF-8」）。
"""
# --- 输出编码保护（Windows 控制台默认 GBK，会让 emoji/中文崩溃）---
try:
    import sys as _sys
    for _s in (_sys.stdout, _sys.stderr):
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import csv
import io
import json
import sys
from pathlib import Path

# 表头别名
ALIAS = {
    "id": ("id", "编号", "记录号"),
    "param": ("param", "参数", "参数名"),
    "value": ("value", "数值", "值"),
    "unit": ("unit", "单位"),
    "date": ("date", "日期", "时间"),
    "source": ("source", "来源", "出处"),
    "scope": ("scope", "样本", "批次", "样品"),
    "superseded_by": ("superseded_by", "被取代", "取代者", "作废于"),
}
REQUIRED = ("id", "param", "value", "unit")


def norm_header(h: str) -> str:
    h = (h or "").strip().lstrip("\ufeff")
    low = h.lower().replace(" ", "").replace("_", "")
    for key, names in ALIAS.items():
        for n in names:
            if low == n.lower().replace(" ", ""):
                return key
    return low


def to_float(raw: str):
    t = str(raw or "").strip().replace(",", "")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src = Path(sys.argv[1])
    if not src.exists():
        print(f"找不到文件：{src}")
        return 1
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".jsonl")

    raw = src.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        print("无法识别文件编码，请另存为 UTF-8 或 GBK")
        return 1

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        print("CSV 没有表头")
        return 1
    mapping = {h: norm_header(h) for h in reader.fieldnames}
    missing = [k for k in REQUIRED if k not in mapping.values()]
    if missing:
        print(f"缺少必需列：{missing}")
        print(f"识别到的表头：{mapping}")
        return 1

    rows, bad = [], []
    for i, r in enumerate(reader, start=2):
        rec = {mapping[k]: (v or "").strip() for k, v in r.items() if k in mapping}
        if not any(rec.values()):
            continue
        val = to_float(rec.get("value"))
        if val is None:
            bad.append((i, rec.get("value"), "数值不是数字"))
            continue
        if not rec.get("unit"):
            bad.append((i, rec.get("param"), "缺单位"))
            continue
        out = {"id": rec.get("id") or f"R{i:03d}", "param": rec.get("param"),
               "value": val, "unit": rec.get("unit")}
        for k in ("date", "source", "scope", "superseded_by"):
            if rec.get(k):
                out[k] = rec[k]
        rows.append(out)

    with dst.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"已转换：{len(rows)} 条 → {dst}")
    if bad:
        print(f"跳过 {len(bad)} 行（有问题）：")
        for ln, v, why in bad[:8]:
            print(f"  第 {ln} 行：{why}（值={v!r}）")
    if rows:
        print("\n转换结果预览（前 3 条）：")
        for r in rows[:3]:
            print("  " + json.dumps(r, ensure_ascii=False))
    print("\n下一步：用 lint_records.py 检查规范")
    return 0 if not bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
