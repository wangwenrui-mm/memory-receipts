"""配置读写（config.json，放在应用根目录）。

设计原则：
- 文件不存在时自动生成默认配置；
- 任何字段缺失都回落到内置默认值，不会因为缺行崩溃；
- 只读写本文件，不碰系统环境、不写注册表。
"""
# --- 输出编码保护（Windows 控制台默认 GBK，会让中文/符号崩溃）---
try:
    import sys as _sys
    for _s in (_sys.stdout, _sys.stderr):
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]     # 应用根目录
CONF = BASE / "config.json"

DEFAULT = {
    "app": {
        "name": "lab-verify",
        "version_file": "VERSION",
    },
    # ---- 预留的大模型接口：默认关闭，填了 key 并置 enabled=true 才生效 ----
    "api": {
        "enabled": False,
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key": "",
        "timeout": 60,
        "max_tokens": 2048,
        "temperature": 0.0,
        "note": "任意 OpenAI 兼容接口都可以：base_url 填到 /v1 之前或之后都行，程序会自动补 /chat/completions",
    },
    "verify": {
        "use_llm_extract": False,   # 是否用大模型做抽取（需要 api.enabled=true）
    },
}


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    if not CONF.exists():
        save(DEFAULT)
        return json.loads(json.dumps(DEFAULT))
    try:
        raw = json.loads(CONF.read_text(encoding="utf-8-sig"))
    except Exception:
        raw = {}
    return _deep_merge(DEFAULT, raw)


def save(cfg: dict):
    CONF.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def update(section: str, values: dict):
    cfg = load()
    cfg.setdefault(section, {}).update(values)
    save(cfg)
    return cfg


def api_ready() -> bool:
    a = load().get("api") or {}
    return bool(a.get("enabled")) and bool(str(a.get("api_key") or "").strip())


def main():
    import sys
    cfg = load()
    if "--show" in sys.argv or not sys.argv[1:]:
        a = cfg["api"]
        print("=== 当前配置 ===")
        print(f"  文件：{CONF}")
        print(f"  API 开关：{'开' if a['enabled'] else '关'}　模型：{a['model']}")
        print(f"  接口地址：{a['base_url']}")
        key = str(a.get("api_key") or "")
        print(f"  API Key：{(key[:6] + '…' + key[-4:]) if len(key) > 12 else ('(未填)' if not key else '(已填)')}")
        print(f"  用大模型抽取：{'是' if cfg['verify']['use_llm_extract'] else '否'}")
        print("\n  要启用：编辑 config.json，把 api.enabled 改为 true，api.api_key 填上你的 key")
        return 0
    if "--set-key" in sys.argv:
        key = sys.argv[sys.argv.index("--set-key") + 1]
        update("api", {"api_key": key, "enabled": True})
        print("已保存 API Key 并启用")
        return 0
    if "--disable" in sys.argv:
        update("api", {"enabled": False})
        print("已关闭 API")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
