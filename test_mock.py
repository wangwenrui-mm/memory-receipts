#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地联调测试：不需要真 QQ。

用法： python test_mock.py            # 文字测试
      python test_mock.py --image    # 图片理解测试（会现造一张带文字的图片）

原理：起一个假 OneBot 服务端 -> 桥接连上来 -> 伪造私聊消息 -> 打印桥接发回的回复。
"""
import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import websockets

import bridge

TEST_UIN = 9999999
PORT = 3999
IMG_PORT = 8099
IMG_DIR = Path(__file__).resolve().parent / "data" / "testimg"
SECRET = "QQ-BOT-9527"

received: list[dict] = []


def make_image() -> Path:
    from PIL import Image, ImageDraw

    IMG_DIR.mkdir(parents=True, exist_ok=True)
    p = IMG_DIR / "test.png"
    img = Image.new("RGB", (600, 200), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, 590, 190], outline="black", width=4)
    d.text((60, 30), "secret code:", fill="black", font_size=28)
    d.text((60, 80), SECRET, fill="black", font_size=64)
    img.save(p)
    return p


def serve_image(path: Path):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", IMG_PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def private_msg(message: list, raw: str) -> str:
    return json.dumps({
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "self_id": 1000000001, "user_id": TEST_UIN, "message_id": 10001,
        "message": message, "raw_message": raw,
        "sender": {"user_id": TEST_UIN, "nickname": "测试"},
    }, ensure_ascii=False)


async def fake_napcat(ws):
    await asyncio.sleep(0.5)
    await ws.send(private_msg(
        [{"type": "text", "data": {"text": "你好，用一句话介绍你自己"}}],
        "你好，用一句话介绍你自己",
    ))
    if len(sys.argv) > 1 and sys.argv[1] == "--image":
        await asyncio.sleep(0.5)
        await ws.send(private_msg(
            [
                {"type": "image", "data": {"url": f"http://127.0.0.1:{IMG_PORT}/test.png"}},
                {"type": "text", "data": {"text": "这张图里写的是什么？照抄那串大写字母和数字"}},
            ],
            "[图片]这张图里写的是什么？",
        ))

    async for raw in ws:
        msg = json.loads(raw)
        received.append(msg)
        await ws.send(json.dumps({
            "status": "ok", "retcode": 0,
            "data": {"message_id": 20000 + len(received)},
            "echo": msg.get("echo"),
        }, ensure_ascii=False))


async def main():
    cfg = bridge.load_config()
    cfg.napcat_ws = f"ws://127.0.0.1:{PORT}"
    cfg.napcat_token = ""
    cfg.owner_qq = [TEST_UIN]
    cfg.auto_bind_first_sender = False

    bridge.setup_logging("INFO")
    b = bridge.Bridge(cfg)

    want = 1
    if len(sys.argv) > 1 and sys.argv[1] == "--image":
        want = 2
        p = make_image()
        serve_image(p)
        print(f"[test] 测试图片: {p}  上面写着 {SECRET}")

    async with websockets.serve(fake_napcat, "127.0.0.1", PORT):
        task = asyncio.create_task(b.run_forever())
        for _ in range(150):
            await asyncio.sleep(1)
            if len(received) >= want:
                await asyncio.sleep(1)
                break
        task.cancel()

    print("\n" + "=" * 64)
    if not received:
        print("❌ 桥接没有发出任何消息")
        return 1
    for i, m in enumerate(received):
        texts = [s["data"]["text"] for s in m["params"].get("message", []) if s.get("type") == "text"]
        print(f"[{i + 1}] {m['action']}: {''.join(texts)}")

    ok = True
    if want == 2:
        alltext = " ".join(
            s["data"]["text"]
            for m in received
            for s in m["params"].get("message", [])
            if s.get("type") == "text"
        )
        if SECRET in alltext:
            print(f"\n✅ 看图成功：模型读出了图里的 {SECRET}")
        else:
            ok = False
            print(f"\n⚠️ 回复里没出现 {SECRET}（模型没照抄，或看图没生效）")

    cnt, cost, toks = bridge.usage_summary(1)
    print(f"用量: {cnt} 次调用, {toks} tokens, 约 ¥{cost:.5f}")
    b.store.reset(f"u{TEST_UIN}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
