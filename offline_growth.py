"""离线成长：每周用日记 + 情绪原话重写她的「自我描述」（不依赖 QQ）。

用法：
    python offline_growth.py           # 到期才写，没到期就跳过
    python offline_growth.py --force   # 强制写一次
    python offline_growth.py --dry     # 只看状态和素材，不调模型、不写文件
"""
import sys
import json
import time
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import bridge  # noqa: E402

USER_QQ = "u" + str((CFG.get("owner_qq") or ""))


def _frozen() -> bool:
    """完全冻结：项目根目录存在 FROZEN 文件时，连计划任务也不执行。

    PAUSE 只是停 QQ（日记/成长继续，这是设计）；FROZEN 是全部停下。
    """
    return (Path(__file__).parent / "FROZEN").exists()


def main():
    if _frozen():
        print("已完全冻结（存在 FROZEN 文件），本次跳过。")
        return 0
    dry = "--dry" in sys.argv
    force = "--force" in sys.argv

    cfg = bridge.load_config()
    b = bridge.Bridge(cfg)
    if not b.api_key:
        print("没有 API key，无法重写自我描述")
        return 1

    g = cfg.raw.get("growth") or bridge.DEFAULT_CONFIG["growth"]
    days = float(g.get("soul_rewrite_days", 7))
    last = float(b.persona.d.get("soul_updated") or 0)
    due = (not last) or (time.time() - last >= days * 86400)

    print("=" * 56)
    print("【成长检查】")
    if last:
        print(f"  上次重写: {time.strftime('%Y-%m-%d %H:%M', time.localtime(last))}")
        print(f"  间隔设置: {days:g} 天")
        print(f"  是否到期: {'是' if due else '否'}")
    else:
        print("  还没重写过（第一次）")

    cur = (b.persona.d.get("soul") or "").strip()
    print(f"  当前自我描述: {len(cur)} 字")

    diaries = (b.persona.d.get("diary") or [])[-7:]
    moments = (b.mem.rec(USER_QQ).get("moments") or [])[-8:]
    print(f"  素材: 日记 {len(diaries)} 篇 / 情绪原话 {len(moments)} 条")
    if diaries:
        print("  最近日记:")
        for d in diaries[-3:]:
            print(f"    - {d.get('date')}: {(d.get('text') or '')[:40]}…")
    traits = b.persona.d.get("traits") or {}
    print("  性格数值: " + "、".join(f"{k}{int(v)}" for k, v in traits.items()))
    print("=" * 56)

    if dry:
        print("（--dry 模式，不调用模型、不写文件）")
        return 0

    if not due and not force:
        print("没到期，跳过。要强制写一次加 --force")
        return 0

    if not diaries and not moments:
        print("素材太少（没有日记也没有情绪原话），跳过")
        return 0

    before = cur
    asyncio.run(b._maybe_grow())
    after = (b.persona.d.get("soul") or "").strip()

    if after == before:
        print("这次没有产生新的自我描述（模型没返回或出错，看日志）")
        return 1

    print()
    print("【新的自我描述】")
    print(after)
    print()
    print(f"（{before.__len__()} 字 → {len(after)} 字，已写入 persona.json，历史留在 soul_log）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
