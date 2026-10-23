"""预留的大模型接口（默认关闭）。

特点：
- 只用标准库（urllib），不需要 pip install openai；
- 任何 OpenAI 兼容接口都能接（DeepSeek / 通义 / Kimi / 本地 ollama / vLLM 都可）；
- 没配置时所有函数安全返回，不会让主流程崩；
- 有 `--selftest` 做离线自检（不发请求，只验证配置与拼装）。

用法：
    python llm.py --status          看状态
    python llm.py --test            发一条测试请求（会消耗极少 token）
    python llm.py --selftest        离线自检（不发请求）
"""

from __future__ import annotations
# --- 输出编码保护（Windows 控制台默认 GBK，会让中文/符号崩溃）---
try:
    import sys as _sys
    for _s in (_sys.stdout, _sys.stderr):
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE.parent))
from lab.src import config as C  # noqa: E402


def status() -> dict:
    a = C.load().get("api") or {}
    return {
        "enabled": bool(a.get("enabled")),
        "ready": C.api_ready(),
        "base_url": a.get("base_url") or "",
        "model": a.get("model") or "",
        "has_key": bool(str(a.get("api_key") or "").strip()),
        "use_llm_extract": bool((C.load().get("verify") or {}).get("use_llm_extract")),
    }


def _endpoint(base_url: str) -> str:
    u = (base_url or "").rstrip("/")
    if u.endswith("/chat/completions"):
        return u
    if u.endswith("/v1"):
        return u + "/chat/completions"
    return u + "/v1/chat/completions"


def chat(messages: list[dict], max_tokens: int | None = None,
         temperature: float | None = None) -> str | None:
    """调用大模型；未启用或出错时返回 None（调用方应回退到本地逻辑）。"""
    if not C.api_ready():
        return None
    a = C.load()["api"]
    url = _endpoint(a.get("base_url") or "")
    body = {
        "model": a.get("model") or "deepseek-chat",
        "messages": messages,
        "max_tokens": int(max_tokens or a.get("max_tokens") or 2048),
        "temperature": float(temperature if temperature is not None
                             else a.get("temperature", 0.0)),
        "stream": False,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + str(a.get("api_key") or "").strip())
    try:
        with urllib.request.urlopen(req, timeout=float(a.get("timeout") or 60)) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  [API] HTTP {e.code}：{e.read()[:200].decode('utf-8', 'replace')}")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"  [API] 请求失败：{e}")
        return None
    try:
        obj = json.loads(raw)
        return obj["choices"][0]["message"]["content"]
    except Exception:
        print("  [API] 返回格式无法解析")
        return None


def extract_claims(text: str, limit: int = 40) -> list[dict] | None:
    """用大模型从文本里抽「参数 / 数值 / 单位」。未启用返回 None。

    返回形如 [{"param":"退火温度","value":850,"unit":"℃"}, ...]
    """
    if not (C.api_ready() and (C.load().get("verify") or {}).get("use_llm_extract")):
        return None
    prompt = (
        "从下面的文本里抽取所有「参数 + 数值 + 单位」的测量陈述。\n"
        "只输出 JSON 数组，不要解释。每个元素：\n"
        '{"param":"参数名（中文优先，保留原文用词）","value":数值(数字),"unit":"单位"}\n'
        f"最多 {limit} 条。没有则输出 []。\n\n文本：\n" + (text or "")[:6000]
    )
    out = chat([{"role": "user", "content": prompt}], max_tokens=1500)
    if not out:
        return None
    s = out.strip()
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s[3:] else s.strip("`")
        s = s.replace("json", "", 1).strip()
    try:
        arr = json.loads(s)
    except Exception:
        return None
    rows = []
    for it in arr if isinstance(arr, list) else []:
        try:
            rows.append({"param": str(it["param"]), "value": float(it["value"]),
                         "unit": str(it["unit"])})
        except Exception:
            continue
    return rows


def selftest() -> int:
    """离线自检：验证配置读取、URL 拼装、降级行为。不发网络请求。"""
    ok = 0
    total = 0

    def check(name, cond):
        nonlocal ok, total
        total += 1
        ok += bool(cond)
        print(f"  {'✅' if cond else '❌'} {name}")

    st = status()
    check("能读到配置", isinstance(st, dict) and "enabled" in st)
    check("默认关闭（不联网）", st["enabled"] is False or st["has_key"] is True)
    check("URL 拼装 /v1/chat/completions", _endpoint("https://api.deepseek.com").endswith("/v1/chat/completions"))
    check("URL 已含 /v1 不重复", _endpoint("https://x.com/v1") == "https://x.com/v1/chat/completions")
    check("URL 已含完整路径不重复", _endpoint("https://x.com/v1/chat/completions").count("/v1") == 1)
    check("未配置时 chat 返回 None", chat([{"role": "user", "content": "hi"}]) is None)
    check("未配置时 extract 返回 None", extract_claims("退火温度 850 ℃") is None)
    print(f"离线自检通过 {ok}/{total}")
    return 0 if ok == total else 1


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if "--status" in sys.argv:
        st = status()
        print("=== API 状态 ===")
        print(f"  开关：{'开' if st['enabled'] else '关'}")
        print(f"  可用：{'是' if st['ready'] else '否（未填 key 或未启用）'}")
        print(f"  地址：{st['base_url'] or '(空)'}")
        print(f"  模型：{st['model'] or '(空)'}")
        print(f"  用大模型抽取：{'是' if st['use_llm_extract'] else '否'}")
        return 0
    if "--test" in sys.argv:
        if not C.api_ready():
            print("尚未配置 API（config.json 里 api.enabled=false 或 key 为空）")
            return 1
        print("发送测试请求……")
        out = chat([{"role": "user", "content": "只回复两个字：可用"}], max_tokens=16)
        if out:
            print("返回：", out.strip()[:60])
            print("✅ 接口可用")
            return 0
        print("❌ 接口不可用（看上面错误信息）")
        return 1
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
