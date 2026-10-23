"""建立论文检索索引：把论文里的「参数 + 数值 + 单位」全抽出来，供快速查找。

用法：
    python build_index.py                # 用 lab/data/corpus（内置 1000 篇 arXiv 摘要）
    python build_index.py <目录>          # 用你自己的论文目录（.xml / .txt / .md）

输出：
    lab/data/index/papers.jsonl          一行一条数值记录（含论文标题、日期、分类、原句）
    lab/data/index/summary.json          统计信息（篇数、条数、参数 Top、单位 Top）

支持的文件：
    .xml   arXiv Atom 格式（含 <title>/<summary>/<published>）
    .txt   / .md  纯文本，文件名当标题，文件修改时间当日期
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
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE.parent))
from lab.src import verify as V  # noqa: E402

NS = {"a": "http://www.w3.org/2005/Atom"}

# 英文参数词 → 中文分类（用于中文检索；配对仍以"离数字最近的名词"为准）
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

STOP = {"approximately", "about", "and", "for", "from", "with", "that", "this",
        "least", "most", "rate", "than", "also", "into", "over", "under", "their",
        "which", "while", "have", "been", "were", "such", "between", "using", "based",
        "the", "of", "in", "to", "at", "on", "by", "as", "is", "are", "was", "near",
        "around", "up", "down", "below", "above", "followed", "via", "within",
        "reaches", "operates", "exceeding", "shows", "give", "gives", "reported",
        "measured", "found", "after", "before", "per", "each", "both", "all"}


def load_papers(src: Path) -> list[dict]:
    papers = []
    files = sorted(src.glob("*.xml")) + sorted(src.glob("*.txt")) + sorted(src.glob("*.md"))
    for f in files:
        if f.suffix.lower() == ".xml":
            try:
                root = ET.parse(f).getroot()
            except Exception as e:
                print(f"  ⚠️ 跳过 {f.name}：{e}")
                continue
            entries = root.findall("a:entry", NS)
            if not entries:
                continue
            for i, e in enumerate(entries):
                title = (e.findtext("a:title", "", NS) or "").strip().replace("\n", " ")
                text = (e.findtext("a:summary", "", NS) or "").strip().replace("\n", " ")
                date = (e.findtext("a:published", "", NS) or "")[:10]
                pid = (e.findtext("a:id", "", NS) or "").split("/")[-1] or f"{f.stem}-{i}"
                if text:
                    papers.append({"paper_id": pid, "title": title, "date": date,
                                   "cat": f.stem, "text": text})
        else:
            text = f.read_text(encoding="utf-8", errors="ignore").strip()
            if not text:
                continue
            mtime = time.strftime("%Y-%m-%d", time.localtime(f.stat().st_mtime))
            papers.append({"paper_id": f.stem, "title": f.stem, "date": mtime,
                           "cat": f.parent.name, "text": text})
    return papers


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "data" / "corpus"
    if not src.exists():
        print(f"找不到目录：{src}")
        return 1
    out_dir = BASE / "data" / "index"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 按源目录名分库，避免互相覆盖
    lib = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in src.resolve().name)[:40] or "default"
    lib_file = out_dir / f"{lib}.jsonl"

    papers = load_papers(src)
    print(f"扫描 {src}：{len(papers)} 篇")

    rows = []
    t0 = time.time()
    for p in papers:
        for sent in re.split(r"(?<=[.;。；])\s*", p["text"]):
            for c in V.extract(sent):
                cands = [x for x in (c.get("param_cands") or [])
                         if x and x.lower() not in STOP and len(x) >= 2]
                longest = max(cands, key=len) if cands else ""
                zh = WORD2ZH.get((longest or "").lower(), "")
                pname = longest or "(未识别参数)"
                rows.append({
                    "paper_id": p["paper_id"], "title": p["title"], "date": p["date"],
                    "cat": p["cat"],
                    "param": pname,
                    "param_raw": longest,
                    "param_zh": zh,          # 中文分类（可空）
                    "param_category": zh,
                    "param_cands": (c.get("param_cands") or [])[:5],
                    "value": c.get("value") if c["kind"] == "point" else None,
                    "lo": c.get("lo"), "hi": c.get("hi"),
                    "unit": c.get("unit_raw") or c.get("unit") or "",
                    "kind": c["kind"], "raw": c.get("raw") or "",
                    "context": (c.get("context") or "")[:200],
                })
    dt = time.time() - t0

    with lib_file.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    params = Counter(r.get("param") for r in rows if r.get("param"))
    units = Counter(r["unit"] for r in rows if r["unit"])
    summary = {
        "built_at": time.strftime("%Y-%m-%d %H:%M"),
        "source": str(src), "papers": len(papers), "records": len(rows),
        "with_value": sum(1 for r in rows if r["value"] is not None),
        "params_top": params.most_common(15), "units_top": units.most_common(15),
        "elapsed_s": round(dt, 1),
    }
    (out_dir / f"{lib}.summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    # 记住最近一次建的库，供 search 默认使用
    (out_dir / "current.txt").write_text(lib, encoding="utf-8")

    print(f"抽出数值记录 {len(rows)} 条（其中单值 {summary['with_value']} 条），耗时 {dt:.1f}s")
    print(f"索引已写入：{lib_file.name}（库名：{lib}）")
    print(f"参数 Top5：{params.most_common(5)}")
    print(f"单位 Top5：{units.most_common(5)}")
    print("\n下一步：python lab/src/search.py 温度")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
