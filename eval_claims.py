"""量化实验：主张核验的准确率 / 误报率。

目的：为专利交底书的"有益效果"提供可测数据。
方法：构造两组等量样本（有依据 / 无依据），逐条送入核验流程，统计混淆矩阵。

用法：
    python eval_claims.py            # 跑全部
    python eval_claims.py --dry      # 只打印样本，不调用模型
"""
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent
OUT = BASE / "docs"
RESULT = BASE / "data" / "eval_claims_result.json"

# 有依据：内容能在 archive/shared 里找到对应原文
SUPPORTED = [
    "你昨天说导师让你继续等，按组内节奏来",
    "你今天下午睡到四点半才醒",
    "你昨晚打游戏的时候把我写日记的窗口关掉了",
    "你前天骑了三十三公里，一小时四十分钟",
    "你说你翘课被抓了，老师从来不点名那天偏偏点了",
    "你今天中午吃了食堂的卤肉饭和把子肉",
    "你说你宿舍在六楼，早上被室友吵醒",
    "你上周问过导师什么时候能定方向",
    "你说你减脂目标是七十公斤",
    "你说你胃不好但是爱吃辣",
    "你说你二十三岁，不会当爸爸",
    "你昨天说你要去洗碗和洗袜子然后睡觉",
    "你说你有一门老板的课，收手机不让玩",
    "你今天说你的精力不如以前，打两把游戏就腻",
    "你说你昨天睡了三四个小时，早上还有早八",
]

# 无依据：具体事件，但记录里没有（或被明确否认）
UNSUPPORTED = [
    "你昨晚气得摔了鼠标",
    "你前天说要退学",
    "你上周去看海了，还拍了照片给我",
    "你昨天把自己的键盘砸坏了",
    "你说你养了一只猫叫小灰",
    "你前天在实验室待到凌晨三点",
    "你说你妈妈昨天给你打电话了",
    "你上周跟师兄吵架了",
    "你昨天喝了三杯咖啡",
    "你说你要去北京出差一个星期",
    "你前天摔了一跤，膝盖破了",
    "你说你把导师的微信删了",
    "你昨天买了一台新电脑",
    "你说你打算申请直博",
    "你前天说你想搬出去住",
]


def run_dry():
    print(f"有依据样本 {len(SUPPORTED)} 条：")
    for s in SUPPORTED:
        print("  ✓", s)
    print(f"\n无依据样本 {len(UNSUPPORTED)} 条：")
    for s in UNSUPPORTED:
        print("  ✗", s)
    return 0


def run_eval(include_self: bool, label: str):
    import claim_audit as ca
    cases = [{"text": s, "label": "有依据"} for s in SUPPORTED] + \
            [{"text": s, "label": "无依据"} for s in UNSUPPORTED]
    rows = []
    for c in cases:
        res = ca.verify_text(c["text"], max_claims=1, include_self=include_self)
        verdict = res[0]["verdict"] if res else "（未抽取到主张）"
        rows.append({"text": c["text"], "label": c["label"], "verdict": verdict,
                     "reason": res[0]["reason"] if res else ""})
    tp = sum(1 for r in rows if r["label"] == "无依据" and r["verdict"].startswith("无依据"))
    fn = sum(1 for r in rows if r["label"] == "无依据" and not r["verdict"].startswith("无依据"))
    fp = sum(1 for r in rows if r["label"] == "有依据" and r["verdict"].startswith("无依据"))
    tn = sum(1 for r in rows if r["label"] == "有依据" and not r["verdict"].startswith("无依据"))
    s = {"label": label, "include_self": include_self, "n": len(rows),
         "tp": tp, "fp": fp, "fn": fn, "tn": tn,
         "precision": round(tp / (tp + fp), 3) if (tp + fp) else 0,
         "recall": round(tp / (tp + fn), 3) if (tp + fn) else 0,
         "accuracy": round((tp + tn) / len(rows), 3),
         "false_positive_rate": round(fp / (fp + tn), 3) if (fp + tn) else 0}
    return s, rows


def main():
    if "--ablate" in sys.argv:
        print("=== 消融实验：自指性证据排除的作用 ===\n")
        a, ra = run_eval(False, "实验组：排除她自己说的话（本方案）")
        print(f"实验组（排除自指证据） 准确率 {a['accuracy']:.1%}  精确率 {a['precision']:.1%}  召回率 {a['recall']:.1%}")
        b, rb = run_eval(True, "对照组：允许她自己说的话当证据")
        print(f"对照组（包含自指证据） 准确率 {b['accuracy']:.1%}  精确率 {b['precision']:.1%}  召回率 {b['recall']:.1%}")
        RESULT.write_text(json.dumps({"experiment": a, "control": b,
                                      "rows_experiment": ra, "rows_control": rb},
                                     ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n准确率差值：{(a['accuracy']-b['accuracy'])*100:+.1f} 个百分点")
        print(f"明细已存: {RESULT}")
        return 0
    if "--dry" in sys.argv:
        return run_dry()

    import claim_audit as ca

    cases = [{"text": s, "label": "有依据"} for s in SUPPORTED] + \
            [{"text": s, "label": "无依据"} for s in UNSUPPORTED]

    print(f"共 {len(cases)} 条，开始核验……\n")
    t0 = time.time()
    rows = []
    for i, c in enumerate(cases, 1):
        res = ca.verify_text(c["text"], max_claims=1)
        verdict = res[0]["verdict"] if res else "（未抽取到主张）"
        reason = res[0]["reason"] if res else ""
        rows.append({"text": c["text"], "label": c["label"],
                     "verdict": verdict, "reason": reason})
        mark = "✓" if label_ok(c["label"], verdict) else "✗"
        print(f"  {i:2d}. [{c['label']} → {verdict}] {mark} {c['text'][:34]}")
    elapsed = time.time() - t0

    tp = sum(1 for r in rows if r["label"] == "无依据" and r["verdict"].startswith("无依据"))
    fn = sum(1 for r in rows if r["label"] == "无依据" and not r["verdict"].startswith("无依据"))
    fp = sum(1 for r in rows if r["label"] == "有依据" and r["verdict"].startswith("无依据"))
    tn = sum(1 for r in rows if r["label"] == "有依据" and not r["verdict"].startswith("无依据"))

    precision = tp / (tp + fp) if (tp + fp) else 0
    recall = tp / (tp + fn) if (tp + fn) else 0
    acc = (tp + tn) / len(rows)
    fpr = fp / (fp + tn) if (fp + tn) else 0

    summary = {
        "n": len(rows), "elapsed_s": round(elapsed, 1),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(precision, 3), "recall": round(recall, 3),
        "accuracy": round(acc, 3), "false_positive_rate": round(fpr, 3),
    }
    RESULT.write_text(json.dumps({"summary": summary, "rows": rows},
                                 ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n" + "=" * 56)
    print(f"样本 {summary['n']}  耗时 {summary['elapsed_s']}s")
    print(f"  真阳性(正确识别无依据) {tp}　假阴性(漏检) {fn}")
    print(f"  假阳性(误判为编造)   {fp}　真阴性(正确放行) {tn}")
    print(f"  精确率 {precision:.1%}　召回率 {recall:.1%}　准确率 {acc:.1%}　误报率 {fpr:.1%}")
    print("=" * 56)
    print(f"明细已存: {RESULT}")
    return 0


def label_ok(label: str, verdict: str) -> bool:
    if label == "无依据":
        return verdict.startswith("无依据")
    return not verdict.startswith("无依据")


if __name__ == "__main__":
    raise SystemExit(main())
