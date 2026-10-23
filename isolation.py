"""小鲨（个人实例）与科研版（实验室实例）的物理隔离。

目标：两个实例的数据在任何情况下都不互相流入。
- 目录分离：data/（个人）与 lab/data/（科研）
- 配置分离：profiles/personal.json 与 profiles/lab.json
- 访问守卫：任何以 lab 身份运行的程序，读取个人目录直接抛异常（反之亦然）
- 检查工具：isolation_check.py 定期扫描有无越界文件

用法：
    from isolation import data_root, guard
    root = data_root("lab")          # → C:\\qq-deepseek\\lab\\data
    guard("lab", some_path)          # 越界抛 IsolationError
"""

from __future__ import annotations
from pathlib import Path

BASE = Path(__file__).parent

PERSONAL_DATA = BASE / "data"           # 小鲨的全部数据（含隐私）
LAB_DATA = BASE / "lab" / "data"        # 科研版数据（脱敏/合成/公开材料）

PERSONAL_MARKERS = ("persona.json", "diary_offline.jsonl", "shared.jsonl",
                    "dad_letters", "letters_to_dad")


class IsolationError(RuntimeError):
    """跨实例数据访问被拦截。"""


def data_root(profile: str) -> Path:
    p = (profile or "").strip().lower()
    if p in ("personal", "xiaosha", "小鲨"):
        return PERSONAL_DATA
    if p in ("lab", "research", "科研"):
        return LAB_DATA
    raise IsolationError(f"未知 profile：{profile}")


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:  # noqa: BLE001
        return False


def guard(profile: str, path: str | Path) -> Path:
    """检查某路径是否允许被该 profile 访问；不允许则抛异常。"""
    p = Path(path)
    if _is_under(p, PERSONAL_DATA) and data_root(profile) != PERSONAL_DATA:
        raise IsolationError(f"{profile} 不能访问个人数据：{p}")
    if _is_under(p, LAB_DATA) and data_root(profile) != LAB_DATA:
        raise IsolationError(f"{profile} 不能访问科研数据：{p}")
    return p


def check() -> dict:
    """体检：看两个目录有没有互相污染。"""
    issues = []
    if LAB_DATA.is_dir():
        for f in LAB_DATA.rglob("*"):
            if not f.is_file():
                continue
            if any(m in f.name for m in PERSONAL_MARKERS):
                issues.append(f"科研目录里出现个人数据文件：{f}")
    if PERSONAL_DATA.is_dir() and (BASE / "lab").is_dir():
        for f in (BASE / "lab").rglob("*"):
            if f.is_file() and f.suffix in (".jsonl", ".json") and f.name not in ("manifest.json",):
                txt = f.read_text(encoding="utf-8", errors="ignore")[:400]
                if any(k in txt for k in (("助手名", "日记", "家人"))):
                    issues.append(f"科研目录文件疑似含个人内容：{f}")
    return {"personal_root": str(PERSONAL_DATA),
            "lab_root": str(LAB_DATA),
            "personal_exists": PERSONAL_DATA.is_dir(),
            "lab_exists": LAB_DATA.is_dir(),
            "issues": issues,
            "ok": not issues}


def main():
    import sys
    if "--check" in sys.argv:
        r = check()
        print("隔离检查：", "✅ 通过" if r["ok"] else "❌ 发现问题")
        print(f"  个人数据：{r['personal_root']}（存在={r['personal_exists']}）")
        print(f"  科研数据：{r['lab_root']}（存在={r['lab_exists']}）")
        for i in r["issues"]:
            print("  -", i)
        return 0 if r["ok"] else 1
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
