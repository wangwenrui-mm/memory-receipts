"""网页通道：在浏览器里和她说话（手机连同一 Wi-Fi 也能用）。

为什么有这个：
  QQ 通道会被平台限制，控制台通道不方便手机用。
  网页通道用标准库 http.server 起一个本地页面，零额外依赖、零封号风险。

用法：
    pip install websockets
    python chat_web.py                 # 打开 http://127.0.0.1:8765
    python chat_web.py --port 9000
    python chat_web.py --mock          # 假模型，不花钱
    python chat_web.py --lan           # 允许局域网访问（手机可连）

安全提示：
  没有登录验证，默认只监听 127.0.0.1。
  用 --lan 时同网段的人都能访问，仅建议在家里/实验室的私人网络使用。
"""
import argparse
import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --- 输出编码保护 ---
try:
    import sys as _sys
    for _s in (_sys.stdout, _sys.stderr):
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

HANDLE_LOCK = None  # 由 main() 赋值；用于串行化事件循环
MODE_TEXT = "本地运行"  # 由 main() 按真假模型设置，避免误导用户

PAGE = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>小鲨 · 本地对话</title>
<style>
  :root { --bg:#0f1115; --fg:#e8e8ea; --me:#2b6cb0; --her:#1f2430; --dim:#8a8f98; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
         display:flex; flex-direction:column; height:100vh; }
  header { padding:12px 16px; border-bottom:1px solid #23262e; display:flex;
           justify-content:space-between; align-items:center; }
  header b { font-weight:600; letter-spacing:.5px; }
  header span { color:var(--dim); font-size:12px; }
  #log { flex:1; overflow-y:auto; padding:16px; display:flex;
         flex-direction:column; gap:10px; }
  .msg { max-width:82%; padding:9px 13px; border-radius:12px; white-space:pre-wrap;
         word-break:break-word; }
  .me { align-self:flex-end; background:var(--me); }
  .her { align-self:flex-start; background:var(--her); }
  .sys { align-self:center; color:var(--dim); font-size:12px; }
  footer { padding:12px; border-top:1px solid #23262e; display:flex; gap:8px; }
  input { flex:1; padding:11px 13px; border-radius:10px; border:1px solid #2c313c;
          background:#171a21; color:var(--fg); font-size:15px; }
  button { padding:11px 18px; border-radius:10px; border:0; background:var(--me);
           color:#fff; font-size:15px; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
</style></head>
<body>
<header><b>小鲨</b><span id="st">__MODE_TEXT__</span></header>
<div id="log"><div class="sys">连上了。说点什么吧。</div></div>
<footer>
  <input id="q" placeholder="输入消息，回车发送" autocomplete="off">
  <button id="s">发送</button>
</footer>
<script>
const log = document.getElementById('log');
const q = document.getElementById('q');
const s = document.getElementById('s');
const st = document.getElementById('st');
function add(text, cls) {
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  d.textContent = text;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}
async function send() {
  const text = q.value.trim();
  if (!text) return;
  q.value = ''; add(text, 'me');
  s.disabled = true; st.textContent = '她在想…';
  try {
    const r = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text})
    });
    const j = await r.json();
    if (j.reply) add(j.reply, 'her'); else add('（没有回复）' + (j.error||''), 'sys');
  } catch (e) {
    add('请求失败：' + e, 'sys');
  } finally {
    s.disabled = false; st.textContent = '__MODE_TEXT__';
    q.focus();
  }
}
s.onclick = send;
q.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
q.focus();
</script>
</body></html>"""



def _safe(s) -> str:
    """把可能含非法字节的字符串变成可安全打印的文本。"""
    try:
        return str(s).encode("utf-8", "replace").decode("utf-8")
    except Exception:
        return "(无法显示的文本)"
def build_bridge(mock: bool, data_dir: str = ""):
    from bridge import load_config, Bridge
    import bridge as B

    if data_dir:
        # 把所有 DATA 下的路径常量重定向到指定目录，从而复用她的真实记忆
        old_root = Path(getattr(B, "DATA_DIR", Path(".")))
        new_root = Path(data_dir).expanduser().resolve()
        new_root.mkdir(parents=True, exist_ok=True)
        # 把所有「位于旧 data 目录下」的路径常量都重定向
        # （早先只处理 *_PATH，漏掉了 TRAIT_LOG / DECISION_LOG / DAD_LETTERS /
        #   HER_LETTERS / RESTART_FLAG / LOG_DIR，导致这些日志被写丢）
        for name in dir(B):
            val = getattr(B, name, None)
            if not isinstance(val, Path):
                continue
            try:
                rel = val.relative_to(old_root)
            except ValueError:
                continue
            setattr(B, name, new_root / rel)
        B.DATA_DIR = new_root
        print(f"  数据目录：{new_root}")

    cfg = load_config()
    try:
        cfg.raw.setdefault("screen", {})["enable"] = False
    except Exception:
        pass

    class WebBridge(Bridge):
        async def send_text(self, user_id, text, group_id=None):
            # 网页通道由 HTTP 响应带回，不做推送
            self._last_reply = text

        async def send_bubbles(self, user_id, text, group_id=None, first_delay=None):
            self._last_reply = (getattr(self, "_last_reply", "") or "") + text

        async def send_face(self, user_id, face_id, group_id=None):
            pass

        async def send_sticker(self, user_id, group_id=None):
            return True

        async def send_record(self, user_id, wav, group_id=None):
            return True

        async def call(self, action, params, timeout=30):
            return {}

        async def _publish_qzone(self, content, dcfg):
            return None

    br = WebBridge(cfg)
    if mock:
        async def fake_generate(messages):
            n = sum(1 for m in messages if m.get("role") == "user")
            return (f"（假模型回复 · 第 {n} 句）链路是通的：人格已加载、记忆已就绪。"
                    f"接上真实 API 后这里就是她的回答。", {"total_tokens": 0}, 0)
        br._generate = fake_generate
    return br, cfg


def main():
    ap = argparse.ArgumentParser(description="网页通道（本地页面）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--mock", action="store_true", help="假模型，不调用 API")
    ap.add_argument("--lan", action="store_true", help="允许局域网访问（手机可连）")
    ap.add_argument("--dir", default="", help="数据目录（默认用程序目录下的 data/）")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    global HANDLE_LOCK, MODE_TEXT
    HANDLE_LOCK = threading.Lock()
    MODE_TEXT = ("假模型 · 不联网 · 不花钱" if args.mock
                 else "对话经 DeepSeek API（上下文随请求发送）")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bridge, cfg = build_bridge(args.mock, getattr(args, 'dir', ''))

    owners = cfg.raw.get("owner_qq") or []
    if isinstance(owners, int):
        owners = [owners]
    try:
        user_id = int(owners[0]) if owners else 1
    except Exception:
        user_id = 1
    user_id = user_id or 1

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):     # 安静一点
            return

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = PAGE.replace("__MODE_TEXT__", MODE_TEXT).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path != "/api/chat":
                self.send_error(404)
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                # 有些客户端（如 PowerShell 默认、部分国产工具）不发 UTF-8，
                # 这里依次尝试，避免中文变问号或直接 500
                payload = None
                for enc in ("utf-8", "gb18030", "latin-1"):
                    try:
                        payload = raw.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
                if payload is None:
                    payload = raw.decode("utf-8", "replace")
                data = json.loads(payload)
                text = str(data.get("text") or "").strip()
                if not text:
                    self._json({"reply": ""})
                    return
                # 同一个事件循环被多线程同时驱动会报
                # "This event loop is already running"，这里串行化
                with HANDLE_LOCK:
                    bridge._last_reply = ""
                    loop.run_until_complete(bridge.handle(text, user_id))
                    reply = (bridge._last_reply or "").strip()
                self._json({"reply": reply})
            except Exception as e:  # noqa: BLE001
                if args.debug:
                    import traceback
                    traceback.print_exc()
                self._json({"reply": "", "error": f"{type(e).__name__}: {e}"}, 500)

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    try:
        srv = ThreadingHTTPServer((host, args.port), Handler)
    except OSError as e:
        print()
        print(f"  [!] 端口 {args.port} 用不了：{e}")
        print()
        print("  常见原因：")
        print("    · 上一次的窗口没关干净（关掉旧窗口，或结束残留的 python 进程）")
        print("    · 端口被别的程序占用")
        print()
        print(f"  换个端口试试：  python chat_web.py --port {args.port + 1}")
        print()
        return 2
    url = f"http://{'127.0.0.1' if args.lan else host}:{args.port}"
    print("=" * 62)
    print("  网页通道 · 小鲨" + ("（假模型，不花钱）" if args.mock else "（真实模型，会计费）"))
    print(f"  打开：{url}")
    if args.lan:
        print("  局域网已开：手机连同一个 Wi-Fi，用电脑内网 IP 访问")
    print("  Ctrl+C 停止")
    print("=" * 62)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
