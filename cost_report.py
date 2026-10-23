#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""花费小账本：直接运行会打印今日 / 24 小时的 token 与花费。

用法: python cost_report.py        今天
      python cost_report.py 168    最近 168 小时（7 天）
"""
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
USAGE = BASE / "data" / "usage.jsonl"


def load() -> list[dict]:
    if not USAGE.exists():
        return []
    out = []
    for line in USAGE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def agg(rows: list[dict]) -> tuple[int, int, float]:
    toks = sum((r.get("prompt") or 0) + (r.get("completion") or 0) for r in rows)
    return len(rows), toks, sum(float(r.get("cost") or 0) for r in rows)


def line(label: str, rows: list[dict]):
    n, toks, cost = agg(rows)
    if not n:
        print(f"  {label:<10} 无记录")
        return
    print(f"  {label:<10} {n:>4} 次   {toks:>8} tokens   ¥{cost:.4f}   平均 ¥{cost / n:.5f}/次")


def main():
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    rows = load()
    now = dt.datetime.now()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    since = (now - dt.timedelta(hours=hours)).timestamp()

    print("=" * 62)
    print(f"  QQ ↔ DeepSeek 花费账本    {now:%Y-%m-%d %H:%M}")
    print("=" * 62)
    line("今天", [r for r in rows if r["ts"] >= today0])
    line(f"近{hours}小时", [r for r in rows if r["ts"] >= since])

    recent = [r for r in rows if r["ts"] >= since]
    if recent:
        print("\n  分人：")
        by_user = defaultdict(list)
        for r in recent:
            by_user[r.get("user")].append(r)
        for u, rs in sorted(by_user.items(), key=lambda kv: -agg(kv[1])[2]):
            line(str(u), rs)

        print("\n  分类型：")
        by_kind = defaultdict(list)
        for r in recent:
            by_kind[r.get("kind") or "chat"].append(r)
        names = {"chat": "回复对话", "proactive": "主动消息"}
        for k, rs in by_kind.items():
            line(names.get(k, k), rs)

        hi = [r for r in recent if r.get("peak")]
        print(f"\n  高峰时段 {len(hi)} 次 ¥{agg(hi)[2]:.4f} / 空闲 {len(recent) - len(hi)} 次 ¥{agg([r for r in recent if not r.get('peak')])[2]:.4f}")
        print(f"  缓存命中合计 {sum(r.get('hit') or 0 for r in recent)} tokens（这部分几乎不计费）")

    print("\n" + "-" * 62)
    print("  明细（最近 12 条）：")
    for r in rows[-12:]:
        t = dt.datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M:%S")
        kind = {"chat": "对话", "proactive": "主动"}.get(r.get("kind"), "旧")
        u = r.get("user") or "-"
        print(f"  {t}  {kind}  {str(u):<11} in={r.get('prompt') or 0:<5} out={r.get('completion') or 0:<4} ¥{r['cost']:.5f}")
    print("=" * 62)


if __name__ == "__main__":
    main()
