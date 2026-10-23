"""核验一篇文档里的数值型陈述，是否都能在实验记录里溯源。

用法：
    python verify_doc.py <文档路径> [记录文件.jsonl]

支持 .md / .txt / .docx（.docx 需要 python-docx）。
不给记录文件时，默认用同目录下的 records.jsonl。

输出：
    - 每条陈述的判定：有依据 / 已过时 / 矛盾 / 未溯源 / 计划
    - 汇总：各类计数、未溯源率、需要人工确认的条目
退出码：0 = 无矛盾且无未溯源；1 = 存在问题（便于脚本化）
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

BASE = Path(__file__).resolve().parents[1]          # lab/
sys.path.insert(0, str(BASE.parent))
from lab.src import verify as V  # noqa: E402

MARK = {"有依据": "✅", "已过时": "⏳", "矛盾": "❌", "未溯源": "⚠️", "计划": "○"}


def read_doc(path: Path) -> str:
    suf = path.suffix.lower()
    if suf in (".md", ".txt", ".csv", ""):
        return path.read_text(encoding="utf-8", errors="ignore")
    if suf == ".docx":
        try:
            from docx import Document
        except Exception:
            raise SystemExit("读取 .docx 需要 python-docx：pip install python-docx\n"
                             "或先把文档另存为 .txt 再核验。")
        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs]
        for t in doc.tables:                       # 表格里的数值也要核
            for row in t.rows:
                parts.append(" ".join(c.text for c in row.cells))
        return "\n".join(parts)
    raise SystemExit(f"不支持的文件类型：{suf}（支持 .md / .txt / .docx）")


def load_records(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"找不到记录文件：{path}")
    out = []
    for i, ln in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
        ln = ln.strip()
        if not ln:
            continue
        try:
            import json
            out.append(json.loads(ln))
        except Exception:
            print(f"  ⚠️ 记录第 {i} 行不是合法 JSON，已跳过")
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    doc_path = Path(sys.argv[1])
    rec_path = Path(sys.argv[2]) if len(sys.argv) > 2 else BASE / "data" / "records.jsonl"

    if not doc_path.exists():
        print(f"找不到文档：{doc_path}")
        return 1

    text = read_doc(doc_path)
    records = load_records(rec_path)
    claims = V.extract(text)
    results = V.verify(claims, records)

    print(f"文档：{doc_path.name}（{len(text)} 字）")
    print(f"记录：{rec_path.name}（{len(records)} 条）")
    print(f"抽出数值陈述：{len(claims)} 条")
    if not claims:
        print("\n没有发现可核验的数值陈述（需要「参数 + 数值 + 单位」三要素）。")
        print("例：「退火温度 850 ℃」「氧化层厚度为 120 nm」。")
        return 0

    from collections import Counter
    cnt = Counter()
    print("\n" + "=" * 64)
    for r in results:
        cnt[r["state"]] += 1
        raw = (r.get("raw") or "").strip().replace("\n", " ")
        print(f"{MARK[r['state']]} [{r['state']}] {raw[:36]:38s}")
        print(f"     {r['why']}")
    print("=" * 64)

    total = len(results)
    bad = cnt["矛盾"] + cnt["未溯源"]
    print(f"汇总：有依据 {cnt['有依据']}　已过时 {cnt['已过时']}　矛盾 {cnt['矛盾']}　"
          f"未溯源 {cnt['未溯源']}　计划 {cnt['计划']}")
    print(f"未溯源率：{cnt['未溯源'] / total:.1%}　（高于 20% 通常说明参数命名不统一）")

    if cnt["矛盾"]:
        print("\n⚠️ 有矛盾项：说明文档里的数值与记录不一致，请人工确认谁对。")
    if cnt["未溯源"]:
        print("⚠️ 有未溯源项：可能原因——参数名不一致 / 单位不在词表 / 记录未入库。")
        print("   处理建议：先跑 lint_records.py 检查命名规范。")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
