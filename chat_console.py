"""控制台通道：不开 QQ，直接在终端里和她说话。

用途：
  1. 验证核心逻辑（记忆、人格、核验）——不依赖 QQ，也就不会被平台限制
  2. 调试：改完 bridge.py 立刻能试，不用重启 NapCat、不用扫码
  3. 离线演示：--mock 模式下不调用任何 API，零成本

用法：
    pip install websockets            # bridge 模块级依赖
    python chat_console.py            # 真实对话（会调用 DeepSeek，产生费用）
    python chat_console.py --mock     # 假模型，不花钱，只看流程
    python chat_console.py --mock --dir ./sandbox   # 用独立数据目录，不碰正式数据

命令：
    /reset  清空当前对话记忆
    /cost   看花费
    /quit   退出

设计说明：
  bridge.py 里的 Bridge.handle(text, user_id, ...) 本来就与 QQ 无关，
  它只在"发出去"这一步调用 send_text()。所以这里做两件事：
    ① 继承 Bridge，把 send_* 全部改成打印；
    ② 拦截 call()（所有的 OneBot 接口调用），避免误连 QQ。
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

# --- 输出编码保护（Windows 控制台默认 GBK）---
try:
    import sys as _sys
    for _s in (_sys.stdout, _sys.stderr):
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# 让「从本文件所在目录导入 bridge」在任何 Python 发行版下都成立
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))



def _safe(s) -> str:
    """把可能含非法字节的字符串变成可安全打印的文本。"""
    try:
        return str(s).encode("utf-8", "replace").decode("utf-8")
    except Exception:
        return "(无法显示的文本)"
def build_channel(cfg, mock: bool):
    from bridge import Bridge          # 延迟导入，方便先看参数

    class ConsoleBridge(Bridge):
        """把 QQ 通道换成终端。"""

        # ---- 所有"发出去"的动作改成打印 ----
        async def send_text(self, user_id, text, group_id=None):
            if text and text.strip():
                print(f"\n她> {_safe(text).strip()}")

        async def send_bubbles(self, user_id, text, group_id=None, first_delay=None):
            await self.send_text(user_id, text, group_id)

        async def send_face(self, user_id, face_id, group_id=None):
            print(f"  [表情 {face_id}]")

        async def send_sticker(self, user_id, group_id=None) -> bool:
            print("  [表情包]")
            return True

        async def send_record(self, user_id, wav, group_id=None) -> bool:
            print("  [语音]")
            return True

        # ---- 拦截所有 OneBot 调用，绝不连 QQ ----
        async def call(self, action: str, params: dict, timeout: float = 30):
            print(f"  [拦下 QQ 调用: {action}]")
            return {}

        async def _publish_qzone(self, content, dcfg):
            return None

    # 控制台模式关掉手机抓屏，避免刷「找不到 adb」
    try:
        cfg.raw.setdefault("screen", {})["enable"] = False
    except Exception:
        pass

    br = ConsoleBridge(cfg)

    if mock:
        # 把模型换成假的：不联网、不花钱、不改人格
        async def fake_generate(messages):
            # 只回固定文本，不回显 prompt（prompt 含系统上下文，可能带非法字符）
            n = sum(1 for m in messages if m.get("role") == "user")
            return (f"（假模型回复 · 第 {n} 句）收到。链路是通的："
                    f"人格已加载、记忆已就绪，只是没有真的调用模型。",
                    {"total_tokens": 0}, 0)

        br._generate = fake_generate  # type: ignore[method-assign]

    return br


async def main_async(args):
    from bridge import load_config  # 与 bridge 共用同一份配置读取逻辑

    try:
        cfg = load_config()
    except Exception as e:
        print()
        print("  [!] 配置文件读取失败：config.json 可能写坏了。")
        print(f"      原因：{type(e).__name__}: {e}")
        print()
        print("  三种处理：")
        print("    1) 用模板重新生成：copy config.example.json config.json")
        print("    2) 用 JSON 校验工具检查一下 config.json（多半是少了逗号或引号）")
        print("    3) 确认 owner_qq 是数组，如 \"owner_qq\": [123456]")
        print()
        return 1

    if args.dir:
        # 切数据目录：只影响本次运行（不改代码、不改正式数据）
        import bridge as B
        B.DATA_DIR = Path(args.dir).resolve()
        B.DATA_DIR.mkdir(parents=True, exist_ok=True)
        for attr in ("PERSONA_PATH",):
            old = getattr(B, attr)
            setattr(B, attr, B.DATA_DIR / old.name)

    br = build_channel(cfg, args.mock)
    # owner_qq 在配置里是数组，取第一个
    owners = cfg.raw.get("owner_qq") or []
    if isinstance(owners, int):
        owners = [owners]
    try:
        user_id = int(owners[0]) if owners else 1
    except Exception:
        user_id = 1
    user_id = user_id or 1

    print("=" * 60)
    print("  控制台通道 · 小鲨" + ("（假模型模式，不花钱）" if args.mock else "（真实模型，会计费）"))
    # ---- 启动前检查 API Key（不给陌生人看 traceback）----
    if not args.mock:
        key = str((cfg.raw.get("deepseek_key") or "")).strip()
        if not key or key.startswith("sk-你的"):
            print()
            print("  [!] 还没有配置 API Key，无法使用真实模型。")
            print()
            print("  三种选择：")
            print("    1) 先用假模型看看效果（不花钱）：")
            print("         python chat_console.py --mock")
            print("    2) 把 Key 填进 config.json 的 deepseek_key 字段")
            print("    3) 想换别的模型供应商：改 config.json 里的 deepseek_base")
            print()
            print("  Key 申请：https://platform.deepseek.com/")
            print()
            return 1

    print("  命令：/reset 清空对话  /cost 花费  /quit 退出")
    print("=" * 60)

    while True:
        try:
            line = await asyncio.to_thread(input, "\n你> ")
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0
        line = (line or "").strip()
        if not line:
            continue
        if line in {"/quit", "/exit", "quit", "exit"}:
            print("再见。")
            return 0
        try:
            await br.handle(line, user_id)
        except Exception as e:  # noqa: BLE001
            print(f"  [出错] {_safe(type(e).__name__)}: {_safe(e)}")
            if "UnicodeEncodeError" in type(e).__name__ or "surrogate" in str(e):
                print("  提示：这通常是数据文件里有非法字节。可运行 python tools/repair_data.py 修复。")
            if args.debug:
                import traceback
                traceback.print_exc()


def main():
    ap = argparse.ArgumentParser(description="控制台通道（不用 QQ）")
    ap.add_argument("--mock", action="store_true", help="假模型，不调用 API、不花钱")
    ap.add_argument("--dir", default="", help="数据目录（默认用项目下的 data/）")
    ap.add_argument("--debug", action="store_true", help="出错时打印堆栈")
    args = ap.parse_args()
    try:
        return asyncio.run(main_async(args)) or 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
