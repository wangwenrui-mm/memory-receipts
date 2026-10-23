"""她提议改代码 → 你裁决。

她只能"提议"，不能改。每条提议都落成 data/proposals/<id>.json，等你决定。

用法：
    python proposals.py --list              列出所有待裁决
    python proposals.py --show <id>          看某条的详情
    python proposals.py --reject <id> [原因]  驳回
    python proposals.py --apply <id>         批准（会自动备份 + 跑测试，测试不过就回滚）

安全设计：
- 应用前先把目标文件备份到 backup/pre_proposal_<时间>/
- 应用后自动跑 tests.py；不通过就还原
- 每一步都写进 data/proposals/history.jsonl
"""
import sys
import json
import time
import shutil
import subprocess
from pathlib import Path

BASE = Path(__file__).parent
PROP_DIR = BASE / "data" / "proposals"
HIST = PROP_DIR / "history.jsonl"
PY = sys.executable


def _load(pid: str) -> dict | None:
    f = PROP_DIR / f"{pid}.json"
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def _save(rec: dict):
    (PROP_DIR / f"{rec['id']}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")


def _log(rec: dict, action: str, note: str = ""):
    PROP_DIR.mkdir(parents=True, exist_ok=True)
    with HIST.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "id": rec.get("id"), "action": action,
                            "file": rec.get("file"), "note": note}, ensure_ascii=False) + "\n")


def cmd_list():
    if not PROP_DIR.is_dir():
        print("还没有任何提议。")
        return 0
    items = sorted(PROP_DIR.glob("*.json"))
    items = [p for p in items if p.name != "history.jsonl"]
    if not items:
        print("还没有任何提议。")
        return 0
    print(f"共 {len(items)} 条：\n")
    for f in items:
        r = json.loads(f.read_text(encoding="utf-8"))
        flag = "⏳" if r.get("status") == "待裁决" else "✅" if r.get("status") == "已应用" else "❌"
        print(f"  {flag} [{r['id']}] {r.get('file')}")
        print(f"      理由：{(r.get('reason') or '')[:70]}")
    print("\n看详情：python proposals.py --show <id>")
    return 0


def cmd_show(pid: str):
    r = _load(pid)
    if not r:
        print("找不到这条提议。")
        return 1
    print(f"编号：{r['id']}")
    print(f"时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(r['ts']))}")
    print(f"状态：{r.get('status')}")
    print(f"文件：{r.get('file')}")
    print(f"理由：{r.get('reason')}")
    print("\n她写的改动：")
    print(r.get("diff") or "（空）")
    return 0


def cmd_reject(pid: str, why: str = ""):
    r = _load(pid)
    if not r:
        print("找不到这条提议。")
        return 1
    r["status"] = "已驳回"
    r["reject_reason"] = why
    r["decided_at"] = time.time()
    _save(r)
    _log(r, "驳回", why)
    print(f"已驳回 {pid}。")
    return 0


def _run_tests() -> tuple[bool, str]:
    try:
        p = subprocess.run([PY, str(BASE / "tests.py")], capture_output=True,
                           text=True, encoding="utf-8", errors="ignore", timeout=600)
        out = (p.stdout or "") + (p.stderr or "")
        ok = p.returncode == 0 and "失败 0" in out
        tail = "\n".join(out.strip().splitlines()[-6:])
        return ok, tail
    except Exception as e:  # noqa: BLE001
        return False, f"测试运行失败：{e}"


def cmd_apply(pid: str):
    r = _load(pid)
    if not r:
        print("找不到这条提议。")
        return 1
    if r.get("status") != "待裁决":
        print(f"这条已经是「{r.get('status')}」，不再处理。")
        return 1

    target = (BASE / (r.get("file") or "")).resolve()
    if BASE not in target.parents and target.parent != BASE:
        print("❌ 目标文件不在项目目录内，拒绝。")
        return 1
    if not target.exists():
        print("❌ 目标文件不存在，拒绝。")
        return 1
    if target.suffix not in (".py", ".json", ".md", ".ps1", ".bat", ".vbs"):
        print("❌ 不允许改这种文件。")
        return 1

    stamp = time.strftime("%Y%m%d_%H%M%S")
    bk = BASE / "backup" / f"pre_proposal_{stamp}"
    bk.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, bk / target.name)
    print(f"已备份原文件 → {bk / target.name}")

    before = target.read_text(encoding="utf-8", errors="ignore")
    diff = r.get("diff") or ""

    # 支持两种写法：完整新文件，或 "旧行 => 新行" 的替换块
    if diff.strip().startswith("==="):
        print("这种格式还需要人工处理（她给的是说明，不是可直接套用的补丁）。")
        print("建议：把她的想法转成具体改动再应用。")
        return 1

    if "=>" in diff:
        pairs = []
        for line in diff.splitlines():
            if "=>" in line:
                a, b = line.split("=>", 1)
                pairs.append((a.rstrip(), b.rstrip()))
        new_text = before
        missed = 0
        for a, b in pairs:
            if a and a in new_text:
                new_text = new_text.replace(a, b, 1)
            elif a:
                missed += 1
        if missed:
            print(f"⚠️ 有 {missed} 处原文没找到，改动可能不完整。继续前请用 --show 核对。")
            ans = input("仍然应用？(y/N) ").strip().lower()
            if ans != "y":
                print("已取消。")
                return 1
        target.write_text(new_text, encoding="utf-8")
    else:
        print("她给的不是可自动套用的格式，需要你手动改。")
        return 1

    print("已写入，开始跑回归测试……")
    ok, tail = _run_tests()
    print(tail)
    if not ok:
        shutil.copy2(bk / target.name, target)
        print("❌ 测试未通过，已自动回滚到原文件。")
        r["status"] = "测试未过已回滚"
        _save(r)
        _log(r, "应用失败回滚", tail[-300:])
        return 1

    r["status"] = "已应用"
    r["applied_at"] = time.time()
    _save(r)
    _log(r, "应用成功", "")
    print("✅ 测试通过，改动已生效。")
    return 0


def main():
    args = sys.argv[1:]
    if "--list" in args or not args:
        return cmd_list()
    if "--show" in args:
        return cmd_show(args[args.index("--show") + 1])
    if "--reject" in args:
        i = args.index("--reject")
        why = " ".join(args[i + 2:])
        return cmd_reject(args[i + 1], why)
    if "--apply" in args:
        return cmd_apply(args[args.index("--apply") + 1])
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
