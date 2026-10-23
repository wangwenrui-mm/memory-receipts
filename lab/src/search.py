"""快速查论文里的数据：我想看某个参数，就把相关论文和数值调出来。

用法示例：
    python search.py 温度                  # 找所有含"温度"的数据
    python search.py 迁移率 --unit cm²      # 只要 cm² 单位的
    python search.py 效率 --min 20         # 只要数值 ≥20 的
    python search.py band --limit 50       # 英文也能搜
    python search.py 温度 --json           # 输出 JSON（给别的程序用）
    python search.py --stats               # 看索引里有什么

搜索范围：参数名、原句、上下文、论文标题——四者任一命中即算。
"""
# --- 输出编码保护（Windows 控制台默认 GBK，会让中文/符号崩溃）---
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

# 中文查询扩展表（与 build_index 保持一致）
ZH2EN = {
    "温度": ["temperature"], "退火": ["annealing"], "迁移率": ["mobility"],
    "效率": ["efficiency"], "产率": ["yield"], "禁带": ["band gap", "bandgap", "gap"],
    "厚度": ["thickness"], "浓度": ["concentration", "density"], "密度": ["density"],
    "电压": ["voltage"], "电流": ["current"], "电阻": ["resistance", "resistivity"],
    "频率": ["frequency"], "压力": ["pressure", "stress"], "应力": ["stress", "strain"],
    "尺寸": ["size", "diameter"], "粒径": ["particle", "diameter"], "时间": ["time"],
    "能量": ["energy"], "磁场": ["field", "magnetic"], "功率": ["power"],
    "纯度": ["purity"], "透过率": ["transmittance", "transmission"],
    "反射率": ["reflectance", "reflectivity"], "应变": ["strain"], "损耗": ["loss"],
}


WORD2ZH = {
    "temperature": "温度", "annealing": "退火温度", "mobility": "迁移率",
    "efficiency": "效率", "yield": "产率", "gap": "禁带宽度", "bandgap": "禁带宽度",
    "thickness": "厚度", "concentration": "浓度", "density": "密度",
    "voltage": "电压", "current": "电流", "resistance": "电阻",
    "resistivity": "电阻率", "frequency": "频率", "pressure": "压力",
    "stress": "应力", "strain": "应变", "size": "尺寸", "diameter": "粒径",
    "particle": "粒径", "time": "时间", "duration": "时间", "energy": "能量",
    "power": "功率", "purity": "纯度", "transmittance": "透过率",
    "transmission": "透过率", "reflectance": "反射率", "reflectivity": "反射率",
    "loss": "损耗", "field": "磁场", "mass": "质量", "weight": "质量",
    "length": "长度", "scale": "尺度", "angle": "角度", "ratio": "比率",
    "capacitance": "电容", "conductivity": "电导率", "wavelength": "波长",
    "magnetization": "磁化强度", "coercivity": "矫顽力", "hardness": "硬度",
    "modulus": "模量", "porosity": "孔隙率", "thermal": "热学量",
}


def expand(q: str) -> list[str]:
    """中文查询 → [原词, 对应英文参数词...]"""
    out = [q]
    for en, zh in WORD2ZH.items():
        if zh in q or q in zh:
            out.append(en)
    return out
INDEX_DIR = BASE / "data" / "index"


def _pick_index():
    cur = INDEX_DIR / "current.txt"
    if cur.exists():
        name = cur.read_text(encoding="utf-8").strip()
        f = INDEX_DIR / f"{name}.jsonl"
        if f.exists():
            return f, INDEX_DIR / f"{name}.summary.json"
    for f in sorted(INDEX_DIR.glob("*.jsonl")):
        return f, f.with_suffix(".summary.json")
    return None, None


IDX, SUMMARY = _pick_index()


def load_index() -> list[dict]:
    if IDX is None:
        print("还没有索引。请先运行：python lab/src/build_index.py")
        raise SystemExit(2)
    out = []
    for ln in IDX.read_text(encoding="utf-8", errors="ignore").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def parse_args(argv):
    opt = {"q": [], "unit": None, "min": None, "max": None, "limit": 30,
           "json": False, "stats": False, "cat": None, "param": None,
           "index": None, "list": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--unit":
            i += 1; opt["unit"] = argv[i]
        elif a == "--min":
            i += 1; opt["min"] = float(argv[i])
        elif a == "--max":
            i += 1; opt["max"] = float(argv[i])
        elif a == "--limit":
            i += 1; opt["limit"] = int(argv[i])
        elif a == "--cat":
            i += 1; opt["cat"] = argv[i]
        elif a == "--param":
            i += 1; opt["param"] = argv[i]
        elif a == "--json":
            opt["json"] = True
        elif a == "--stats":
            opt["stats"] = True
        elif a == "--index":
            i += 1; opt["index"] = argv[i]
        elif a == "--list":
            opt["list"] = True
        elif a.startswith("-"):
            pass
        else:
            opt["q"].append(a)
        i += 1
    return opt


def show_stats():
    if not SUMMARY.exists():
        print("还没有索引统计，请先运行 build_index.py")
        return
    s = json.loads(SUMMARY.read_text(encoding="utf-8"))
    print("=== 索引概况 ===")
    print(f"  建立时间：{s['built_at']}")
    print(f"  来源：{s['source']}")
    print(f"  论文 {s['papers']} 篇；数值记录 {s['records']} 条（单值 {s['with_value']} 条）")
    print(f"  抽取耗时：{s['elapsed_s']} 秒")
    print("\n  常见参数：")
    for name, n in s["params_top"]:
        print(f"    {name or '(空)':24s} {n}")
    print("\n  常见单位：")
    for u, n in s["units_top"]:
        print(f"    {u or '(空)':12s} {n}")


def match(r: dict, q: str) -> bool:
    # 参数名/分类只和"这个词本身"比，避免整句误配；上下文与标题允许全文匹配
    name_hay = " ".join([str(r.get("param") or ""),
                         str(r.get("param_zh") or "")]).lower()
    full_hay = " ".join([name_hay, str(r.get("raw") or ""),
                         str(r.get("context") or ""),
                         str(r.get("title") or "")]).lower()
    terms = [x.lower() for x in expand(q)]
    if any(term in name_hay for term in terms):
        return True
    # 分类精确命中（温度=温度），不接受"上下文里出现这个词"
    if any(term in (r.get("param_zh") or "").lower() for term in terms):
        return True
    # 中文查询不做全文兜底（否则"磁场"会命中任何提到磁场但数值是别的句子）
    if any("\u4e00" <= ch <= "\u9fff" for ch in q):
        return False
    return any(term in full_hay for term in terms)


def list_indexes():
    print("=== 已有索引库 ===")
    files = sorted(INDEX_DIR.glob("*.jsonl"))
    if not files:
        print("  （还没有，先运行 build_index.py）")
        return
    cur = (INDEX_DIR / "current.txt").read_text(encoding="utf-8").strip() if (INDEX_DIR / "current.txt").exists() else ""
    for f in files:
        n = sum(1 for _ in f.open(encoding="utf-8", errors="ignore"))
        mark = " ← 当前" if f.stem == cur else ""
        print(f"  {f.stem:24s} {n:6d} 条{mark}")


def main():
    global IDX, SUMMARY
    opt = parse_args(sys.argv[1:])
    if opt["list"]:
        list_indexes()
        return 0
    if opt["index"]:
        f = INDEX_DIR / f"{opt['index']}.jsonl"
        if not f.exists():
            print(f"找不到索引库：{opt['index']}（用 --list 看有哪些）")
            return 2
        IDX = f
        SUMMARY = f.with_suffix(".summary.json")
    if opt["stats"] or (not opt["q"] and not opt["unit"] and not opt["param"]):
        show_stats()
        return 0

    rows = load_index()
    hits = []
    for r in rows:
        if opt["q"] and not any(match(r, q) for q in opt["q"]):
            continue
        if opt["param"] and opt["param"].lower() not in (r.get("param") or "").lower():
            continue
        if opt["unit"] and opt["unit"] not in (r.get("unit") or ""):
            continue
        if opt["cat"] and opt["cat"] not in (r.get("cat") or ""):
            continue
        v = r.get("value")
        if opt["min"] is not None and (v is None or v < opt["min"]):
            continue
        if opt["max"] is not None and (v is None or v > opt["max"]):
            continue
        hits.append(r)

    if opt["json"]:
        print(json.dumps(hits[:opt["limit"]], ensure_ascii=False, indent=1))
        return 0

    keyword = " ".join(opt["q"]) or opt["param"] or opt["unit"] or ""
    print(f"=== 搜索「{keyword}」：命中 {len(hits)} 条，显示前 {min(len(hits), opt['limit'])} 条 ===")
    if not hits:
        print("  没有命中。可试试：更短的词（如「温度」而不是「退火温度值」）、--stats 看看有哪些参数。")
        return 0

    print(f"{'参数(分类)':<18}{'数值':>10} {'单位':<7}{'日期':<12}{'论文'}")
    print("-" * 100)
    for r in hits[:opt["limit"]]:
        if r["kind"] == "range":
            val = f"{r['lo']}~{r['hi']}"
        else:
            val = "" if r.get("value") is None else f"{r['value']:g}"
        title = (r.get("title") or "")[:44]
        name = (r.get("param") or "")[:12]
        cat = r.get("param_zh") or ""
        label = f"{name}" + (f"({cat})" if cat else "")
        print(f"{label:<18}{val:>10} {(r.get('unit') or ''):<7}"
              f"{(r.get('date') or ''):<12}{title}")
    if len(hits) > opt["limit"]:
        print(f"\n  ……还有 {len(hits) - opt['limit']} 条，用 --limit 调大")
    print("\n提示：加 --json 可导出；加 --unit / --min / --max 可筛选")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
