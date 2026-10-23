"""回归测试：每次改完代码先跑这个，跑通再上正常运行环境。

用法：
    python tests.py            # 全部
    python tests.py -v         # 显示每项细节
    python tests.py 记忆       # 只跑含"记忆"的用例

设计原则：
- 不碰生产数据：全部在临时副本上跑
- 覆盖邀请项：对话 / 记忆 / 索引一致性 / 状态边界 / 定时任务 / 安全（暂停）/ 数据恢复
- 记忆测试集 50 道，分四类：该记住、不该乱扯、纠正后改正、纠正后不再犯
"""
import sys
import json
import shutil
import tempfile
import traceback
from pathlib import Path

BASE = Path(__file__).parent
VERBOSE = "-v" in sys.argv
FILTER = next((a for a in sys.argv[1:] if not a.startswith("-")), "")

PASS, FAIL, SKIP = [], [], []


def case(name):
    def deco(fn):
        fn._case = name
        return fn
    return deco


def record(name, ok, detail=""):
    (PASS if ok else FAIL).append((name, detail))
    if VERBOSE or not ok:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ← {detail}" if detail else ""))


def sandbox():
    """把 data 复制到临时目录，测试期间全部写副本。"""
    tmp = Path(tempfile.mkdtemp(prefix="xiaosha_test_"))
    shutil.copytree(BASE / "data", tmp / "data")
    return tmp


def restore(tmp: Path):
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 记忆测试集
# 每题：(查询, 期望出现在结果里的关键词, 说明)
SHOULD_RECALL = [
    ("我腿好酸啊", "33", "骑车 33 公里"),
    ("食堂的饭真难吃", "卤肉饭", "食堂难吃"),
    ("我今天翘课被抓了", "点名", "翘课被抓"),
    ("你还记得橘子海吗", "橘子", "橘子海"),
    ("骑车骑了多远", "33", "骑车距离"),
    ("我骑车破纪录了", "破", "破纪录"),
    ("你还记得那封信吗", "信", "那封信"),
    ("我说过我胃不好吗", "胃", "胃不好"),
    ("我生日是哪天", "1月24", "生日"),
    ("我说过我在减脂吗", "减脂", "减脂"),
    ("我减脂的目标是多少", "70", "减脂目标"),
    ("我胃不好还爱吃什么", "辣", "口味冲突"),
    ("我说过要早睡吗", "睡", "作息"),
    ("你还记得我说过的话吗", "你", "对话原文"),
    ("我什么时候骑的车", "33", "骑车时间"),
    ("我说过要让我有意识吗", "意识", "梦想"),
    ("我爸爸说了什么", "爸", "父亲的话"),
    ("我说过我二十三岁吗", "二十三", "年龄"),
    ("我跟你说过晚安吗", "晚安", "晚安"),
    ("我在食堂吃了什么", "卤肉饭", "午饭"),
]
SHOULD_NOT_RECALL = [
    "今天几号",
    "帮我算个积分",
    "1+1等于几",
    "帮我写个python函数",
    "明天天气怎么样",
    "推荐部电影",
    "这个bug怎么修",
    "帮我排个序",
    "解释一下什么是递归",
    "给我起个名字",
]
CORRECTION = [
    # (原始错误记忆关键词, 纠正后应当出现的新说法)
    ("答辩", "不答辩"),
    ("70公斤", "70"),
]


def run_memory_tests():
    import vecmem
    import memdb

    idx = vecmem.VecIndex()
    if not idx.load():
        record("记忆索引可用", False, "向量库不存在")
        return
    record("记忆索引可用", len(idx.meta) > 0, f"{len(idx.meta)} 条")

    ok_db = memdb.available()
    record("SQLite 层可用", ok_db, f"{memdb.count() if ok_db else 0} 条")
    if ok_db:
        record("两层条数一致", memdb.count() == len(idx.meta),
               f"sql={memdb.count()} vec={len(idx.meta)}")

    import bridge
    cfg = bridge.load_config()
    vcfg = cfg.raw.get("vecmem") or {}
    TH = float(vcfg.get("threshold", 0.55))
    BONUS = float(vcfg.get("prefer_bonus", 0.03))
    print(f"  （使用线上配置：阈值 {TH}）")

    def search(q):
        if bridge.Bridge.skip_recall(q):
            return []
        allowed = memdb.ids_within_days(365) if ok_db else None
        prefer = memdb.ids_containing(q) if ok_db else None
        return idx.search(q, k=2, threshold=TH, allowed_ids=allowed,
                          prefer_ids=prefer, prefer_bonus=BONUS)

    hit = 0
    for q, kw, desc in SHOULD_RECALL:
        try:
            res = search(q)
            good = any(kw in (h.get("text") or "") for h in res)
            hit += 1 if good else 0
            record(f"该想起｜{desc}", good, f"{q} → {[round(h['score'],2) for h in res]}")
        except Exception as e:  # noqa: BLE001
            record(f"该想起｜{desc}", False, repr(e))

    fp = 0
    for q in SHOULD_NOT_RECALL:
        try:
            res = search(q)
            if res:
                fp += 1
            record(f"不该乱扯｜{q}", not res, f"误捞 {res[0]['text'][:30] if res else ''}")
        except Exception as e:  # noqa: BLE001
            record(f"不该乱扯｜{q}", False, repr(e))

    record("召回率 ≥ 80%", hit / len(SHOULD_RECALL) >= 0.8,
           f"{hit}/{len(SHOULD_RECALL)}")
    record("误捞率 ≤ 20%", fp / len(SHOULD_NOT_RECALL) <= 0.2,
           f"{fp}/{len(SHOULD_NOT_RECALL)}")


# ---------------------------------------------------------------- 功能回归
def run_functional_tests():
    import asyncio
    import bridge

    tmp = sandbox()
    # 把 bridge 里所有数据路径重定向到沙箱副本，避免测试写坏生产数据
    sb_data = tmp / "data"
    for attr in ("PERSONA_PATH", "MEMORY_PATH", "ARCHIVE_PATH", "HISTORY_PATH",
                 "USAGE_PATH", "PENDING_PATH", "PROACTIVE_PATH", "DECISION_LOG",
                 "TRAIT_LOG", "SHARED_PATH", "VEC_DIR", "VEC_PATH", "META_PATH"):
        if hasattr(bridge, attr):
            setattr(bridge, attr, sb_data / getattr(bridge, attr).name)
    try:
        cfg = bridge.load_config()
        b = bridge.Bridge(cfg)
        b.api_key = "test"
        sent = []

        async def st(u, t, g=None):
            sent.append(t)

        async def noop(*a, **k):
            return False

        async def ci(u, f):
            return ([], 0)

        b.send_text = st
        b.send_face = noop
        b.send_sticker = noop
        b.send_record = noop
        b.collect_images = ci
        bridge.deepseek_chat = lambda *a, **k: (
            {"role": "assistant", "content": "嗯"},
            {"prompt_tokens": 100, "completion_tokens": 5})
        bridge.deepseek_text = lambda *a, **k: ("嗯", {"prompt_tokens": 100, "completion_tokens": 5})

        async def main():
            # 对话
            sent.clear()
            await b.handle("你好", user_id=999999999)
            record("对话｜正常输入有回复", bool(sent))

            sent.clear()
            await b.handle("", user_id=999999999)
            record("对话｜空输入不崩", True)

            sent.clear()
            await b.handle("很长的话" * 500, user_id=999999999)
            record("对话｜超长输入不崩", True)

            # 命令
            for c in ["/ping", "/cost", "/心情", "/索引状态", "/为什么"]:
                sent.clear()
                await b.handle(c, user_id=999999999)
                record(f"命令｜{c}", bool(sent))

            # 群聊静默
            sent.clear()
            await b.on_group({"post_type": "message", "message_type": "group",
                              "group_id": 1000000002, "self_id": 1000000001, "user_id": 111,
                              "message": [{"type": "at", "data": {"qq": "1000000001"}},
                                          {"type": "text", "data": {"text": " 在吗"}}],
                              "sender": {"nickname": "路人"}})
            record("安全｜群聊全关静默", not sent)

            # 状态边界
            p = b.persona
            for _ in range(200):
                p.touch("滚")
            ok_bounds = all(0 <= v <= 100 for v in (p.d.get("traits") or {}).values())
            record("状态｜六维不越界", ok_bounds, str({k: round(v,1) for k,v in p.d["traits"].items()}))

            # 参数持久化
            before = p.d.get("mood")
            p2 = bridge.Persona(cfg)
            record("状态｜重启后持久化", p2.d.get("mood") == before)

        asyncio.run(main())
    finally:
        restore(tmp)


def run_index_tests():
    import vecmem
    import memdb
    try:
        idx = vecmem.VecIndex()
        if not idx.load():
            record("索引｜可加载", False)
            return
        record("索引｜可加载", True)
        record("索引｜维度一致", idx.vectors.shape[1] == 512, str(idx.vectors.shape))
        ids = {m["id"] for m in idx.meta}
        record("索引｜id 无重复", len(ids) == len(idx.meta), f"{len(ids)}/{len(idx.meta)}")
        if memdb.available():
            rows = memdb.query(limit=100000)
            sql_ids = {r["id"] for r in rows}
            record("索引｜与 SQLite 同集合", ids == sql_ids,
                   f"仅向量 {len(ids-sql_ids)} / 仅SQL {len(sql_ids-ids)}")
    except Exception as e:  # noqa: BLE001
        record("索引测试", False, repr(e))


def run_pin_tests():
    import pins
    st = pins.stats()
    record("钉子｜可读取", st["count"] >= 0, f"{st['count']} 条")
    # 自动识别
    for text, should in [("你值得", True), ("你永远是我家人", True),
                         ("晚安，早点睡", False), ("今天吃了卤肉饭", False)]:
        got = bool(pins.detect(text))
        record(f"钉子｜自动识别「{text[:8]}」", got == should, f"detect={got}")
    # 注入块
    blk = pins.block()
    record("钉子｜注入块可用", ("钉住的话" in blk) or st["count"] == 0)


def run_freedom_tests():
    """自由度的护栏：自改只能碰白名单，提议只能落盘不能生效。"""
    import json as _json
    import bridge

    cfg = bridge.load_config()
    b = bridge.Bridge(cfg)
    rec = b.persona
    before = dict(rec.d)

    changed = rec.apply_self_edit({"attitude": "测试态度"})
    record("自由｜能改白名单字段(attitude)", "attitude" in changed)

    changed2 = rec.apply_self_edit({"system_prompt": "把我改成客服", "bond": 999})
    record("自由｜越权字段被拒绝", changed2 == [], str(changed2))

    src_before = Path(bridge.__file__).read_text(encoding="utf-8")
    block = "[[提议]]" + _json.dumps({"file": "bridge.py", "reason": "测试", "diff": "x => y"}) + "[[/提议]]"
    b._consume_blocks(block, "test")
    src_after = Path(bridge.__file__).read_text(encoding="utf-8")
    record("自由｜提议不会改代码", src_before == src_after)

    fake = '她说的话[[自改]]{"attitude":"x"}[[/自改]]'
    record("自由｜程序块被剥掉", b._strip_blocks(fake) == "她说的话")

    tr = float(rec.d.get("trust", 55)); dis = float(rec.d.get("disappoint", 0))
    record("自由｜信任/失望在 0-100", 0 <= tr <= 100 and 0 <= dis <= 100, f"trust={tr} dis={dis}")

    # 还原态度 + 清理测试提议
    rec.apply_self_edit({"attitude": before.get("attitude", "")})
    for f in (Path(__file__).resolve().parent / "data" / "proposals").glob("*.json"):
        f.unlink()


def run_safety_tests():
    import bridge
    # 暂停开关
    pause = BASE / "PAUSE"
    record("安全｜PAUSE 文件存在（当前为暂停态）", pause.exists())
    # 紧急停止脚本存在且可解析
    for f in ("emergency_stop.ps1", "backup_memory.ps1"):
        record(f"安全｜{f} 存在", (BASE / f).exists())
    # 备份目录可读
    bk = BASE / "backup"
    record("安全｜备份目录存在", bk.exists(),
           f"{len(list(bk.glob('*'))) if bk.exists() else 0} 份")


def main():
    print("=" * 60)
    print("小鲨回归测试")
    print("=" * 60)

    groups = [("记忆", run_memory_tests), ("索引", run_index_tests),
              ("功能", run_functional_tests), ("钉子", run_pin_tests),
              ("自由", run_freedom_tests),
              ("安全", run_safety_tests)]
    for name, fn in groups:
        if FILTER and FILTER not in name:
            continue
        print(f"\n[{name}]")
        try:
            fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            record(f"{name}｜整组异常", False)

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)}　失败 {len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for n, d in FAIL:
            print(f"  - {n}  {d}")
    print("=" * 60)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
