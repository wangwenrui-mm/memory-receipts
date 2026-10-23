"""加压测试：比 selftest 更狠，专门找边界崩溃。

跑法：
    python tools/stress.py            # 全部
    python tools/stress.py --fast     # 跳过耗时项（长会话、1万条记忆）

和 selftest 的分工：
    selftest  验证"新手能不能正常用"
    stress    验证"乱来会不会崩"
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail[:200]}" if not ok and detail else ""))
    return ok


def run(args, cwd, stdin_text=None, timeout=120):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run(args, cwd=str(cwd), input=stdin_text, text=True,
                           capture_output=True, timeout=timeout, env=env,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -9, "TIMEOUT"
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def sandbox(name, cfg_patch=None):
    d = Path(tempfile.mkdtemp(prefix=f"xs_st_{name}_"))
    for f in ROOT.glob("*.py"):
        shutil.copy2(f, d / f.name)
    cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8-sig"))
    cfg["owner_qq"] = [1]
    cfg["deepseek_key"] = "sk-dummy"
    if cfg_patch:
        cfg_patch(cfg)
    (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return d


def start_web(d, port, extra=()):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    p = subprocess.Popen([PY, "chat_web.py", "--mock", "--port", str(port), *extra],
                         cwd=str(d), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", env=env)
    for _ in range(50):
        time.sleep(0.3)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
                if r.status == 200:
                    return p
        except Exception:
            continue
    return p


def post(port, payload, timeout=30):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace"))


# ---------- S1 并发 ----------
def t_concurrent():
    print("\n[S1] 网页并发请求（8 线程同时打）")
    d = sandbox("conc")
    port = 8811
    p = start_web(d, port)
    try:
        out = []
        lock = threading.Lock()

        def worker(i):
            try:
                st, j = post(port, {"text": f"并发第{i}条"})
                with lock:
                    out.append((st, bool(j.get("reply"))))
            except Exception as e:  # noqa: BLE001
                with lock:
                    out.append((str(e), False))

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=120)
        ok = sum(1 for st, has in out if st == 200 and has)
        check(f"8 个并发请求都有回复（{ok}/8）", ok == 8, str(out))
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()
        shutil.rmtree(d, ignore_errors=True)


# ---------- S2~S4 畸形请求 ----------
def t_bad_requests():
    print("\n[S2] 畸形请求")
    d = sandbox("bad")
    port = 8812
    p = start_web(d, port)
    try:
        # 1MB 超长
        try:
            st, j = post(port, {"text": "长" * 500000}, timeout=120)
            check("50 万字符请求被处理（不崩）", st == 200, f"status={st}")
        except Exception as e:  # noqa: BLE001
            check("50 万字符请求被处理（不崩）", False, str(e))

        # 非法 JSON
        try:
            post(port, b"{not json")
            check("非法 JSON 返回错误而不是崩溃", False, "居然返回了 200")
        except urllib.error.HTTPError as e:
            check("非法 JSON 返回错误而不是崩溃", e.code >= 400, f"HTTP {e.code}")
        except Exception as e:  # noqa: BLE001
            check("非法 JSON 返回错误而不是崩溃", False, str(e))

        # 404 / 405
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
            check("未知路径返回 404", False, "返回了 200")
        except urllib.error.HTTPError as e:
            check("未知路径返回 404", e.code == 404, f"HTTP {e.code}")

        # 服务还活着吗
        try:
            st, j = post(port, {"text": "还活着吗"})
            check("以上乱来之后服务仍然可用", st == 200 and bool(j.get("reply")))
        except Exception as e:  # noqa: BLE001
            check("以上乱来之后服务仍然可用", False, str(e))
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()
        shutil.rmtree(d, ignore_errors=True)


# ---------- S5 诡异输入 ----------
def t_weird_input():
    print("\n[S5] 诡异输入（控制字符 / 零宽 / RTL / null）")
    d = sandbox("weird")
    try:
        cases = {
            "null 字节": "abc\x00def",
            "零宽字符": "a\u200bb\u200dc",
            "RTL 文本": "مرحبا بالعالم",
            "组合 emoji": "👨‍👩‍👧‍👦" * 50,
            "超长无空格": "x" * 8000,
            "全标点": "!@#$%^&*()_+-=[]{}|;':\",./<>?",
            "换行符": "第一行\r\n第二行\r第三行",
        }
        stdin = "\n".join(list(cases.values()) + ["/quit"]) + "\n"
        rc, out = run([PY, "chat_console.py", "--mock", "--dir", str(d / "data")],
                      d, stdin_text=stdin, timeout=240)
        check("诡异输入不崩溃", "Traceback" not in out, out[-400:] if "Traceback" in out else "")
        n = out.count("她>")
        check(f"每类都有回复（{n}/7）", n >= 6, f"只回了 {n} 条")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------- S6 坏配置 ----------
def t_bad_config():
    print("\n[S6] 配置错误")
    d = Path(tempfile.mkdtemp(prefix="xs_st_cfg_"))
    for f in ROOT.glob("*.py"):
        shutil.copy2(f, d / f.name)
    try:
        (d / "config.json").write_text("{ 这不是 json", encoding="utf-8")
        rc, out = run([PY, "chat_console.py", "--mock", "--dir", str(d / "data")],
                      d, stdin_text="/quit\n", timeout=60)
        check("非法 JSON 配置不崩溃", "Traceback" not in out, out[-300:] if "Traceback" in out else "")

        cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8-sig"))
        cfg["owner_qq"] = "123456"          # 应该是数组
        cfg["deepseek_key"] = 12345          # 应该是字符串
        (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        rc, out = run([PY, "chat_console.py", "--mock", "--dir", str(d / "data")],
                      d, stdin_text="你好\n/quit\n", timeout=120)
        check("类型写错的配置不崩溃", "Traceback" not in out, out[-400:] if "Traceback" in out else "")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------- S7 长会话 ----------
def t_long_session(turns=150):
    print(f"\n[S7] 长会话（{turns} 轮）")
    d = sandbox("long")
    try:
        stdin = "\n".join([f"第{i}句，随便聊聊" for i in range(turns)] + ["/quit"]) + "\n"
        t0 = time.time()
        rc, out = run([PY, "chat_console.py", "--mock", "--dir", str(d / "data")],
                      d, stdin_text=stdin, timeout=max(300, turns * 4))
        dt = time.time() - t0
        n = out.count("她>")
        check(f"{turns} 轮不崩溃", "Traceback" not in out, out[-300:] if "Traceback" in out else "")
        check(f"{turns} 轮都有回复（{n}）", n >= turns - 3, f"只回了 {n}")
        check(f"{turns} 轮耗时 < {turns * 3} 秒", dt < turns * 3, f"{dt:.0f}s")
        # 数据体量
        dp = d / "data"
        if dp.exists():
            size = sum(f.stat().st_size for f in dp.rglob("*") if f.is_file())
            check(f"数据体量合理（{size / 1024:.0f} KB / {turns} 轮）", size < 20 * 1024 * 1024)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------- S8 大记忆库 ----------
def t_big_memory():
    print("\n[S8] 大记忆库（1 万条）启动")
    d = sandbox("bigmem")
    try:
        data = d / "data"
        data.mkdir(exist_ok=True)
        with (data / "memory.jsonl").open("w", encoding="utf-8") as f:
            for i in range(10000):
                f.write(json.dumps({"ts": time.time(), "kind": "fact",
                                    "text": f"第{i}条记忆内容，用于压测启动速度"},
                                   ensure_ascii=False) + "\n")
        t0 = time.time()
        rc, out = run([PY, "chat_console.py", "--mock", "--dir", str(data)],
                      d, stdin_text="你好\n/quit\n", timeout=180)
        dt = time.time() - t0
        check(f"1 万条记忆下能启动（{dt:.1f}s）", "她>" in out, out[-300:])
        check(f"启动+一轮对话 < 60 秒", dt < 60, f"{dt:.0f}s")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------- S9 路径含空格与中文 ----------
def t_odd_path():
    print("\n[S9] 路径含空格与中文")
    base = Path(tempfile.gettempdir()) / "小鲨 测试 目录"
    d = base / "app"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    d.mkdir(parents=True)
    for f in ROOT.glob("*.py"):
        shutil.copy2(f, d / f.name)
    cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8-sig"))
    cfg["owner_qq"] = [1]
    cfg["deepseek_key"] = "sk-dummy"
    (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    try:
        rc, out = run([PY, "chat_console.py", "--mock", "--dir", str(d / "data")],
                      d, stdin_text="你好\n/quit\n", timeout=120)
        check("中文+空格路径能正常运行", "她>" in out, out[-300:])
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main():
    fast = "--fast" in sys.argv
    print("=" * 66)
    print("  加压测试 · 小鲨")
    print(f"  Python {sys.version.split()[0]}　目录 {ROOT}")
    print("=" * 66)
    t_concurrent()
    t_bad_requests()
    t_weird_input()
    t_bad_config()
    t_odd_path()
    if not fast:
        t_long_session()
        t_big_memory()

    bad = [r for r in results if not r[1]]
    print("\n" + "=" * 66)
    print(f"  共 {len(results)} 项，通过 {len(results) - len(bad)}，失败 {len(bad)}")
    for name, ok, detail in bad:
        print(f"    ✗ {name}  —— {detail[:180]}")
    print("=" * 66)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
