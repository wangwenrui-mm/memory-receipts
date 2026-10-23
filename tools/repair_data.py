"""修复数据文件里的非法字节（UTF-8 解码错误 / 代理字符）。

什么时候需要：
  控制台或网页通道报 UnicodeEncodeError，提示 "surrogates not allowed"。
  通常是从别的系统导入的历史数据里有非 UTF-8 字节。

用法：
    python tools/repair_data.py            # 只检查，不改
    python tools/repair_data.py --fix      # 就地修复（先备份）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TEXT_EXT = {".json", ".jsonl", ".txt", ".md"}


def main():
    fix = "--fix" in sys.argv
    bad = 0
    total = 0
    for f in sorted(DATA.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in TEXT_EXT:
            continue
        total += 1
        raw = f.read_bytes()
        try:
            raw.decode("utf-8")
            continue
        except UnicodeDecodeError:
            pass
        bad += 1
        fixed = raw.decode("utf-8", "replace")
        print(f"  {f.relative_to(ROOT)}  有非法字节")
        if fix:
            bak = f.with_suffix(f.suffix + ".bak_repair")
            bak.write_bytes(raw)
            f.write_text(fixed, encoding="utf-8")
            print(f"    已修复（原文件备份到 {bak.name}）")
    print(f"\n检查 {total} 个文本文件，{bad} 个有问题")
    if bad and not fix:
        print("加 --fix 就地修复")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())