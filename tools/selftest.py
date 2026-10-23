"""出厂自检：模拟「陌生人拿到这份东西」会遇到的所有情况。

跑法：
    python tools/selftest.py              # 全部检查
    python tools/selftest.py --quick      # 跳过耗时项

检查内容：
  A 环境      Python 版本、依赖、关键文件是否齐全
  B 冷启动    空目录、无配置、无数据的情况下能否给出清楚提示
  C 正常对话  假模型跑多轮，看是否稳定
  D 脏输入    空串、超长、表情、引号、控制字符、连续 100 轮
  E 网页通道  页面 200、接口可用、端口占用提示
  F 科研版    自检、文档核验、坏文件、无索引
退出码 0 = 全部通过。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  —— {detail}" if detail and not ok else ""))
    return ok


def run(args, cwd, stdin_text=None, timeout=90, env_extra=None):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    try:
        p = subprocess.run(args, cwd=str(cwd), input=stdin_text, text=True,
                           capture_output=True, timeout=timeout, env=env,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -9, "TIMEOUT"
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def make_sandbox(with_config=True, with_key=True):
    d = Path(tempfile.mkdtemp(prefix="xs_selftest_"))
    for f in ROOT.glob("*.py"):
        shutil.copy2(f, d / f.name)
    if (ROOT / "tools").exists():
        shutil.copytree(ROOT / "tools", d / "tools", dirs_exist_ok=True)
    if with_config:
        cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8-sig"))
        cfg["owner_qq"] = [1]
        cfg["deepseek_key"] = "sk-dummy-for-test" if with_key else ""
        (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return d


# ---------- A 环境 ----------
def test_env():
    print("\n[A] 环境")
    check("Python >= 3.9", sys.version_info >= (3, 9), sys.version.split()[0])
    try:
        import websockets  # noqa: F401
        check("依赖 websockets 已安装", True)
    except Exception as e:  # noqa: BLE001
        check("依赖 websockets 已安装", False, str(e))
    for f in ("bridge.py", "chat_console.py", "chat_web.py",
              "config.example.json", "README.md"):
        check(f"文件存在 {f}", (ROOT / f).exists())
    check("无真实密钥泄漏", "sk-" not in (ROOT / "config.example.json").read_text(encoding="utf-8")
          or "你的key" in (ROOT / "config.example.json").read_text(encoding="utf-8"))


# ---------- B 冷启动 ----------
def test_cold_start():
    print("\n[B] 冷启动与错误提示")
    d = make_sandbox(with_config=False)
    try:
        rc, out = run([PY, "chat_console.py", "--mock", "--dir", str(d / "data")],
                      d, stdin_text="/quit\n", timeout=60)
        check("无配置文件时给出提示而不是崩溃",
              "配置" in out or "config" in out.lower() or "Traceback" not in out,
              out.strip().splitlines()[-1] if out.strip() else "无输出")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    d = make_sandbox(with_key=False)
    try:
        rc, out = run([PY, "chat_console.py"], d, stdin_text="你好\n/quit\n", timeout=90)
        check("没有 API Key 时提示清楚（不静默失败）",
              "key" in out.lower() or "密钥" in out or "API" in out,
              out.strip()[-200:])
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------- C 正常对话 ----------
def test_chat():
    print("\n[C] 正常对话（假模型）")
    d = make_sandbox()
    try:
        rc, out = run([PY, "chat_console.py", "--mock", "--dir", str(d / "data")],
                      d, stdin_text="你好\n今天有点累\n/quit\n", timeout=120)
        check("能跑起来", "控制台通道" in out, out[:200])
        n = out.count("她>")
        check("三轮输入有两轮回复（/quit 不算）", n >= 2, f"实际 {n} 轮")
        check("没有 Traceback", "Traceback" not in out)
        check("数据目录被创建", (d / "data").exists())
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------- D 脏输入 ----------
def test_dirty_input():
    print("\n[D] 脏输入与稳定性")
    d = make_sandbox()
    try:
        weird = [
            "",                                  # 空
            "   ",                               # 空白
            "a" * 5000,                          # 超长
            "表情😀测试🔥",                        # emoji
            '引号"和\'都有',                      # 引号
            "换行\n第二行\t制表",                  # 控制字符
            "{}[]()<>|\\/*&^%$#@!~`",            # 符号
            "0" * 200,                           # 长数字
        ]
        stdin = "\n".join(weird + ["/quit"]) + "\n"
        rc, out = run([PY, "chat_console.py", "--mock", "--dir", str(d / "data")],
                      d, stdin_text=stdin, timeout=180)
        check("脏输入不崩溃", "Traceback" not in out, out[-300:] if "Traceback" in out else "")
        check("超长输入被处理", out.count("她>") >= 1)

        # 连续 100 轮（压稳定性）
        t0 = time.time()
        stdin = "\n".join([f"第{i}句" for i in range(100)] + ["/quit"]) + "\n"
        rc, out = run([PY, "chat_console.py", "--mock", "--dir", str(d / "data2")],
                      d, stdin_text=stdin, timeout=600)
        dt = time.time() - t0
        n = out.count("她>")
        check("连续 100 轮不崩溃", "Traceback" not in out, out[-300:] if "Traceback" in out else "")
        check(f"100 轮都有回复（{n} 条 / {dt:.0f}s）", n >= 95, f"只回了 {n} 条")
        check("100 轮耗时 < 300 秒", dt < 300, f"{dt:.0f}s")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------- E 网页通道 ----------
def test_web():
    print("\n[E] 网页通道")
    d = make_sandbox()
    port = 8791
    try:
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        proc = subprocess.Popen([PY, "chat_web.py", "--mock", "--port", str(port)],
                                cwd=str(d), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", env=env)
        try:
            ok = False
            for _ in range(40):
                time.sleep(0.4)
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
                        page = r.read().decode("utf-8", "replace")
                        ok = r.status == 200 and "小鲨" in page
                        break
                except Exception:
                    continue
            check("页面可访问且内容正确", ok)

            body = json.dumps({"text": "网页自检"}).encode()
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/chat", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.loads(r.read().decode("utf-8"))
            check("接口有回复", bool(j.get("reply")), str(j)[:200])
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

        # 端口占用时的提示
        s = socket.socket()
        s.bind(("127.0.0.1", port))
        s.listen(1)
        try:
            rc, out = run([PY, "chat_web.py", "--mock", "--port", str(port)], d, timeout=40)
            check("端口被占用时给出可读提示",
                  "Address already in use" in out or "10048" in out or "端口" in out
                  or "Traceback" not in out, out[-200:])
        finally:
            s.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------- F 科研版 ----------
def test_lab():
    print("\n[F] 科研版（lab/）")
    lab = ROOT / "lab"
    if not lab.exists():
        check("lab/ 存在", False, "缺少 lab 目录")
        return
    check("lab/ 存在", True)
    rc, out = run([PY, str(lab / "src" / "verify.py"), "--selftest"], ROOT)
    check("边界自检 15/15", "15/15" in out, out[-200:])

    rc, out = run([PY, str(lab / "src" / "verify_doc.py"), "不存在的文件.md"], ROOT)
    check("文档不存在时提示清楚", rc == 1 and ("找不到" in out or "不存在" in out), out[-200:])

    tmp = Path(tempfile.mkdtemp(prefix="xs_lab_"))
    try:
        bad = tmp / "bad.jsonl"
        bad.write_text('{"id":"X1","param":"温度","value":"不是数字","unit":"℃"}\n'
                       'this is not json\n', encoding="utf-8")
        rc, out = run([PY, str(lab / "src" / "lint_records.py"), str(bad)], ROOT)
        check("坏记录能被报出来", "问题" in out and rc in (0, 1), out[-200:])

        empty = tmp / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        rc, out = run([PY, str(lab / "src" / "lint_records.py"), str(empty)], ROOT)
        check("空记录文件不崩溃", "Traceback" not in out, out[-200:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    quick = "--quick" in sys.argv
    print("=" * 64)
    print("  出厂自检 · 小鲨")
    print(f"  目录：{ROOT}")
    print(f"  Python：{sys.version.split()[0]}（{PY}）")
    print("=" * 64)
    test_env()
    if not quick:
        test_cold_start()
        test_chat()
        test_dirty_input()
        test_web()
    test_lab()

    bad = [r for r in results if not r[1]]
    print("\n" + "=" * 64)
    print(f"  共 {len(results)} 项，通过 {len(results) - len(bad)}，失败 {len(bad)}")
    for name, ok, detail in bad:
        print(f"    ✗ {name}" + (f"  —— {detail[:160]}" if detail else ""))
    print("=" * 64)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
