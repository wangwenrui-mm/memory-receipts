"""SQLite 结构化记忆层：给向量检索加一道"确定性护栏"。

它不做语义猜测，只做精确过滤：
- 按时间范围筛（最近 N 天）
- 按类型筛（日记 / 对话 / 他说的话 / 信件）
- 按关键词是否出现筛（用于给相关记录加权）

设计原则：
- 只读源文件，不改任何原始数据
- 幂等写入（INSERT OR REPLACE），重复跑不会产生重复行
- 唯一会清空的是它自己的表，且只在 --rebuild 时
"""

from __future__ import annotations
import sys
import json
import time
import sqlite3
from pathlib import Path

BASE = Path(__file__).parent
DATA = BASE / "data"
DB_PATH = DATA / "memory.db"


def _connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS rec (
            id     TEXT PRIMARY KEY,
            date   TEXT,
            ts     REAL,
            type   TEXT,
            source TEXT,
            text   TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_date ON rec(date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_type ON rec(type)")
    return con


def _chunks():
    """复用 vecmem 的切块逻辑，两个索引永远同源。"""
    try:
        import vecmem
        return vecmem.all_chunks()
    except Exception:  # noqa: BLE001
        return []


def _parse_ts(date_str: str) -> float:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d_%H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(date_str[:len("2026-10-21 19:15")], fmt)) if fmt != "%Y-%m-%d" else time.mktime(time.strptime(date_str[:10], fmt))
        except Exception:  # noqa: BLE001
            continue
    return 0.0


def sync() -> int:
    """把新切块写入库（幂等）。返回新增/更新条数。"""
    items = _chunks()
    if not items:
        return 0
    con = _connect()
    try:
        cur = con.cursor()
        n = 0
        for it in items:
            cur.execute(
                "INSERT OR REPLACE INTO rec (id, date, ts, type, source, text) VALUES (?,?,?,?,?,?)",
                (it["id"], it.get("date", ""), _parse_ts(it.get("date", "")),
                 it.get("type", ""), it.get("source", ""), it.get("text", "")),
            )
            n += 1
        con.commit()
        return n
    finally:
        con.close()


def rebuild() -> int:
    """清空并全量重建（只影响本表）。"""
    con = _connect()
    try:
        con.execute("DELETE FROM rec")
        con.commit()
    finally:
        con.close()
    return sync()


# ---------------------------------------------------------------- 查询
def count() -> int:
    con = _connect()
    try:
        return int(con.execute("SELECT COUNT(*) FROM rec").fetchone()[0])
    finally:
        con.close()


def stats() -> dict:
    con = _connect()
    try:
        rows = con.execute("SELECT type, COUNT(*) FROM rec GROUP BY type").fetchall()
        total = con.execute("SELECT COUNT(*) FROM rec").fetchone()[0]
        dmin, dmax = con.execute("SELECT MIN(date), MAX(date) FROM rec").fetchone()
        return {"count": int(total), "types": {r[0]: int(r[1]) for r in rows},
                "from": dmin, "to": dmax,
                "size_kb": round(DB_PATH.stat().st_size / 1024, 1) if DB_PATH.exists() else 0}
    finally:
        con.close()


def ids_within_days(days: int) -> set[str] | None:
    """最近 N 天内记录的所有 id；days<=0 表示不限。"""
    if days <= 0:
        return None
    cutoff = time.time() - days * 86400
    con = _connect()
    try:
        rows = con.execute("SELECT id FROM rec WHERE ts >= ? OR ts = 0", (cutoff,)).fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


def ids_containing(text: str, min_len: int = 2) -> set[str]:
    """文本里出现了哪些记录（用连续二字片段做粗匹配，纯本地）。"""
    t = (text or "").strip()
    if len(t) < min_len:
        return set()
    grams = {t[i:i + 2] for i in range(len(t) - 1)}
    grams = {g for g in grams if g.strip()}
    if not grams:
        return set()
    con = _connect()
    try:
        rows = con.execute("SELECT id, text FROM rec").fetchall()
    finally:
        con.close()
    hit = set()
    for rid, rtext in rows:
        rt = rtext or ""
        if any(g in rt for g in grams):
            hit.add(rid)
    return hit


def query(contains: str = "", type_: str = "", since: str = "", until: str = "",
          limit: int = 20) -> list[dict]:
    sql = "SELECT id, date, type, text FROM rec WHERE 1=1"
    args: list = []
    if contains:
        sql += " AND text LIKE ?"
        args.append(f"%{contains}%")
    if type_:
        sql += " AND type = ?"
        args.append(type_)
    if since:
        sql += " AND date >= ?"
        args.append(since)
    if until:
        sql += " AND date <= ?"
        args.append(until + " 99")
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(int(limit))
    con = _connect()
    try:
        rows = con.execute(sql, args).fetchall()
    finally:
        con.close()
    return [{"id": r[0], "date": r[1], "type": r[2], "text": r[3]} for r in rows]


# ---------------------------------------------------------------- 全局
_DB_OK: bool | None = None


def available() -> bool:
    global _DB_OK
    if _DB_OK is None:
        try:
            _DB_OK = DB_PATH.exists() and count() > 0
        except Exception:  # noqa: BLE001
            _DB_OK = False
    return _DB_OK


def main():
    args = sys.argv[1:]
    if "--rebuild" in args:
        n = rebuild()
        print(f"重建完成：{n} 条")
        print("状态:", stats())
        return 0
    if "--sync" in args:
        n = sync()
        print(f"同步完成：{n} 条")
        print("状态:", stats())
        return 0
    if "--status" in args:
        print("状态:", stats())
        return 0
    if "--query" in args:
        q = args[args.index("--query") + 1]
        rows = query(contains=q, limit=10)
        print(f"含「{q}」的记录：{len(rows)} 条")
        for r in rows:
            print(f"  [{r['date']} {r['type']}] {r['text'][:70]}")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
