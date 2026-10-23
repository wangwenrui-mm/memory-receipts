#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键启动 / 进程守护。

- 已经在跑 NapCat（6099 被占用）就不重复启动
- 没登录 QQ 时自动弹出二维码图片（napcat/cache/qrcode.png），扫码即登录
- 二维码换新时会再弹一次
- NapCat / bridge 任一挂掉，3 秒后自动拉起
- 关掉窗口或 Ctrl+C 结束

用法： python run.py
"""
from __future__ import annotations

import os
import socket
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
NAPCAT_DIR = BASE / "NapCat"
NAPCAT_BAT = NAPCAT_DIR / "napcat.bat"
BRIDGE = BASE / "bridge.py"
QR_PNG = NAPCAT_DIR / "napcat" / "cache" / "qrcode.png"
WEBUI = "http://127.0.0.1:6099/webui?token=a892c3b8e942"
QQ_UIN = os.environ.get("QQ_UIN", "")  # 填你的机器人 QQ 号，或用环境变量 QQ_UIN
NAPCAT_ARGS = ["cmd", "/c", str(NAPCAT_BAT), "-q", QQ_UIN]

CREATE_NEW_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
_LOCK = None


def log(msg: str):
    line = f"[supervisor] {msg}"
    print(line, flush=True)
    try:
        (BASE / "logs").mkdir(exist_ok=True)
        with open(BASE / "logs" / "supervisor.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except Exception:  # noqa: BLE001
        pass


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) == 0


def spawn(name: str, args: list[str], cwd: Path) -> subprocess.Popen:
    log(f"启动 {name}")
    return subprocess.Popen(
        args, cwd=str(cwd), stdout=sys.stdout, stderr=subprocess.STDOUT,
        creationflags=CREATE_NEW_GROUP,
    )


# 硬上限：本项目的 node 进程最多 2 个（QQ 主进程 + worker）。
# 超过就绝不再生成；需要更多时由用户明确要求再放开。
MAX_NODE = 2


def _our_node_pids() -> list[int]:
    """只统计属于本项目的 node（不碰用户别的 node 程序）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'node.exe' -and "
             "$_.CommandLine -and $_.CommandLine -like '*qq-deepseek*' } | "
             "ForEach-Object { $_.ProcessId }"],
            capture_output=True, text=True, timeout=25, check=False,
        )
        out = []
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                out.append(int(line))
        return out
    except Exception:  # noqa: BLE001
        return []


def node_count() -> int:
    return len(_our_node_pids())


def trim_nodes(keep: int = MAX_NODE, why: str = "") -> int:
    """超过上限就杀掉多余的（优先杀最新的），返回杀掉的个数。"""
    pids = _our_node_pids()
    if len(pids) <= keep:
        return 0
    extra = sorted(pids)[keep:]  # PID 越大越新
    killed = 0
    for pid in extra:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15, check=False)
            killed += 1
        except Exception:  # noqa: BLE001
            pass
    if killed:
        log(f"⚠️ node 进程超限（{len(pids)} > {keep}），已清掉 {killed} 个" + (f"（{why}）" if why else ""))
    return killed


def kill_tree(proc: subprocess.Popen | None, why: str = ""):
    """整棵进程树一起杀。只 terminate 会留下 node 孤儿，堆积后会互相踢 QQ 下线。"""
    if proc is None or proc.poll() is not None:
        return
    if why:
        log(f"结束进程树 PID {proc.pid}（{why}）")
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=20, check=False)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def kill_stray_napcat():
    """启动前清掉上次残留的 node（避免多个 QQ 实例互相踢）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'node.exe' -and "
             "$_.CommandLine -and $_.CommandLine -like '*qq-deepseek*' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def open_qr(path: Path):
    """复制一份再打开：Windows 照片查看器会锁住文件，导致 NapCat 写不进新二维码。"""
    if not path.exists():
        return
    try:
        dst = path.with_name(f"qrcode_{int(time.time())}.png")
        shutil.copy2(path, dst)
        os.startfile(str(dst))  # type: ignore[attr-defined]
        log("已弹出二维码图片")
        # 只留最近 5 份，免得堆一堆
        olds = sorted(path.parent.glob("qrcode_*.png"), key=lambda x: x.stat().st_mtime)
        for f in olds[:-5]:
            try:
                f.unlink()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        log(f"打不开二维码图片（请手动打开 {path}）: {e}")


def watch_login(stop: threading.Event, procs: dict):
    """一直盯着登录状态：
    - 掉线后先自动重试快速登录（重启 NapCat 会带 -q）
    - 快速登录救不回来再走扫码，换码就弹图
    """
    logged = False
    banner_shown = False
    last_mtime = None
    fresh_at = time.time()
    offline_since = time.time()
    quick_tries = 0
    qr_regen = 0
    last_nag = 0.0
    webui_opened = False
    while not stop.is_set():
        online = port_open(3001)

        if online:
            if not logged:
                log("QQ 已登录，OneBot 端口 3001 就绪 ✅")
            logged = True
            banner_shown = False
            webui_opened = False
            quick_tries = 0
            qr_regen = 0
            offline_since = time.time()
            stop.wait(5)
            continue

        if logged or quick_tries == 0:
            if logged:
                log("⚠️ QQ 掉线了（被踢下线或网络断），尝试自动恢复…")
            logged = False
            offline_since = time.time()

        # QQ 底层网络断了（会话仍显示在线但收发全废）：bridge 写标记，这里负责重启
        flag = BASE / "data" / "napcat_restart.flag"
        if flag.exists():
            try:
                ts = float(flag.read_text(encoding="utf-8").strip() or 0)
            except Exception:  # noqa: BLE001
                ts = 0
            try:
                flag.unlink()
            except Exception:  # noqa: BLE001
                pass
            e0 = procs.get("NapCat")
            if time.time() - ts < 600 and e0 and e0[0].poll() is None:
                log("检测到发送持续失败（QQ 网络异常），重启 NapCat 恢复连接…")
                kill_tree(e0[0], "发送失败自愈")
                offline_since = time.time()
                quick_tries = 0
                stop.wait(20)
                continue

        entry = procs.get("NapCat")

        # 阶段一：掉线 90 秒还没自动回来 → 重启 NapCat（带 -q 快速登录），最多 3 次
        if time.time() - offline_since > 90 and quick_tries < 3 and entry and entry[0].poll() is None:
            quick_tries += 1
            log(f"自动恢复：用快速登录重连 NapCat（第 {quick_tries}/3 次）")
            kill_tree(entry[0], "掉线重启")
            offline_since = time.time()
            stop.wait(25)
            continue

        try:
            mt = QR_PNG.stat().st_mtime if QR_PNG.exists() else None
        except OSError:
            mt = None

        if mt and mt != last_mtime:
            last_mtime = mt
            fresh_at = time.time()
            if not banner_shown:
                log("=" * 52)
                log("快速登录救不回来，需要扫码：用手机 QQ 扫弹出的二维码 你的机器人QQ")
                log(f"也可以浏览器打开： {WEBUI}")
                log("=" * 52)
                banner_shown = True
            open_qr(QR_PNG)
        elif time.time() - fresh_at > 130:
            if entry and entry[0].poll() is None and qr_regen < 4:
                qr_regen += 1
                log(f"二维码过期，换一张（{qr_regen}/4）…")
                kill_tree(entry[0], "换二维码")
            elif not webui_opened:
                webui_opened = True
                try:
                    os.startfile(WEBUI)  # type: ignore[attr-defined]
                    log("二维码可能已过期，已为你打开 WebUI，可在里面重新扫码")
                except Exception:  # noqa: BLE001
                    pass
            fresh_at = time.time()
        # 换码 4 次还没人扫，就停手，只每 10 分钟提醒一次
        if not online and qr_regen >= 4 and time.time() - last_nag > 600:
            last_nag = time.time()
            log("等待人工扫码中（已停止自动换码）。桌面会保留最后一张二维码，或打开 WebUI")
        stop.wait(3)


def main() -> int:
    # 暂停开关：项目目录下存在 PAUSE 文件就什么都不启动（不拉 NapCat、不弹二维码）
    if (BASE / "FROZEN").exists():
        log("已完全冻结（存在 FROZEN 文件），不启动任何进程，也不跑计划任务。")
        return 0

    pause_flag = BASE / "PAUSE"
    if pause_flag.exists():
        log("当前处于暂停状态（存在 PAUSE 文件），不启动任何进程。")
        log("恢复方法：删除 <项目目录>\PAUSE，或双击桌面上的「恢复QQ.bat」")
        return 0

    for p in (NAPCAT_BAT, BRIDGE):
        if not p.exists():
            log(f"找不到 {p}")
            return 1

    # 单实例：防止重复双击造成两个守护进程互相抢端口
    global _LOCK
    _LOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _LOCK.bind(("127.0.0.1", 3012))
        _LOCK.listen(1)
    except OSError:
        log("已有守护进程在运行（127.0.0.1:3012 被占用），本实例退出。")
        return 0

    procs: dict[str, tuple[subprocess.Popen, list[str], Path]] = {}

    kill_stray_napcat()
    time.sleep(1)

    if port_open(6099):
        log("检测到 NapCat 已在运行，跳过启动")
    else:
        n = node_count()
        if n >= MAX_NODE:
            log(f"⚠️ 已有 {n} 个 node 进程（上限 {MAX_NODE}），不再生成新的 NapCat")
            log("   需要继续用的话，先关掉多余的，或者叫我手动放开上限")
        else:
            procs["NapCat"] = (spawn("NapCat", NAPCAT_ARGS, NAPCAT_DIR), NAPCAT_ARGS, NAPCAT_DIR)

    time.sleep(8)
    procs["Bridge"] = (spawn("Bridge", [sys.executable, str(BRIDGE)], BASE),
                       [sys.executable, str(BRIDGE)], BASE)

    stop = threading.Event()
    threading.Thread(target=watch_login, args=(stop, procs), daemon=True).start()

    log("已启动。这个窗口别关，关掉就断线。Ctrl+C 停止。")
    fails = 0
    tick = 0
    try:
        while True:
            time.sleep(5)
            tick += 1
            if tick % 12 == 0:
                trim_nodes(MAX_NODE, "例行检查")   # 硬上限，每 60 秒检查一次
            # NapCat 起来后重置退避
            if "NapCat" in procs and procs["NapCat"][0].poll() is None and port_open(3001):
                fails = 0
            for name, (proc, args, cwd) in list(procs.items()):
                if proc.poll() is not None:
                    kill_tree(proc)   # 清掉可能残留的子进程，防止 node 孤儿堆积
                    if name == "NapCat":
                        # QQ 登录后若立刻重登，容易和上一次会话撞车导致 0xC0000005 崩溃；
                        # 间隔久一点再拉起，成功率明显更高。
                        fails += 1
                        delay = min(60, 8 * (2 ** (fails - 1)))
                        log(f"NapCat 退出(code={proc.returncode})，{delay} 秒后第 {fails} 次重试")
                    else:
                        delay = 3
                        log(f"{name} 退出(code={proc.returncode})，{delay} 秒后重启")
                    time.sleep(delay)
                    if name == "NapCat" and node_count() >= MAX_NODE:
                        log(f"⚠️ node 已达上限 {MAX_NODE}，暂不重启 NapCat")
                        continue
                    procs[name] = (spawn(name, args, cwd), args, cwd)
    except KeyboardInterrupt:
        log("正在停止…")
        stop.set()
        for name, (proc, _, _) in procs.items():
            if proc.poll() is None:
                kill_tree(proc)
        time.sleep(2)
        return 0


if __name__ == "__main__":
    sys.exit(main())
