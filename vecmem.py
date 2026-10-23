"""向量记忆：把她说过的、他说过的、两个人写过的全部切块编码，按语义检索。

用法：
    python vecmem.py --rebuild          重建索引（全量）
    python vecmem.py --sync             增量（只加新的）
    python vecmem.py --status           看索引状态
    python vecmem.py --query "我腿酸"   手动检索测试

设计要点：
- 本地 ONNX（bge-small-zh），不联网、不花钱
- 索引存 data/vec/vectors.npy + meta.jsonl，numpy 暴力检索（几千条只需几毫秒）
- 切块带 date/type，注入时必须标日期，避免"假记忆"
"""

from __future__ import annotations
import sys
import json
import time
import hashlib
import re
from pathlib import Path

import numpy as np

BASE = Path(__file__).parent
DATA = BASE / "data"
VEC_DIR = DATA / "vec"
VEC_PATH = VEC_DIR / "vectors.npy"
META_PATH = VEC_DIR / "meta.jsonl"
MODEL_DIR = BASE / "models" / "bge-small-zh"

# bge-zh 官方建议：查询侧加指令前缀，段落侧不加
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："

# 关键词兜底时忽略的高频二字组
_STOP_GRAMS = {
    "怎么", "什么", "可以", "一个", "今天", "明天", "昨天", "我们", "你们", "他们",
    "这个", "那个", "不是", "就是", "还记", "记得", "一下", "时候", "自己", "没有",
    "现在", "知道", "觉得", "因为", "所以", "但是", "如果", "已经", "一直", "这样",
}


# ---------------------------------------------------------------- 编码器
class Embedder:
    _inst = None

    def __init__(self, model_dir: Path = MODEL_DIR):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.sess = ort.InferenceSession(
            str(model_dir / "model_quantized.onnx"),
            providers=["CPUExecutionProvider"],
        )
        self.tk = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tk.enable_truncation(max_length=512)
        self.tk.enable_padding(pad_id=0, pad_token="[PAD]")
        self._names = [i.name for i in self.sess.get_inputs()]

    @classmethod
    def get(cls) -> "Embedder":
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def embed(self, texts: list[str], batch: int = 16) -> np.ndarray:
        if not texts:
            return np.zeros((0, 512), dtype=np.float32)
        out = []
        for i in range(0, len(texts), batch):
            chunk = texts[i:i + batch]
            encs = self.tk.encode_batch(chunk)
            ids = np.array([e.ids for e in encs], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
            feats = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._names:
                feats["token_type_ids"] = np.zeros_like(ids)
            vec = self.sess.run(None, feats)[0][:, 0]      # CLS pooling
            vec = vec / np.linalg.norm(vec, axis=1, keepdims=True)
            out.append(vec.astype(np.float32))
        return np.vstack(out)

    def embed_queries(self, text: str) -> np.ndarray:
        """返回两套查询向量：原句 + 去掉提问套话后的句子。

        口语里"我说过…吗""你还记得…"这些词对语义匹配是噪声，
        去掉后往往能对上真正的关键词。
        """
        norm = text
        for pat in ("你还记得", "你记得", "我说过", "我说", "你还", "记得",
                    "的时候你说什么", "的时候", "你说什么", "是什么", "有什么",
                    "吗", "呢", "吧", "？", "?", "什么"):
            norm = norm.replace(pat, "")
        norm = norm.strip()
        vecs = [self.embed_query(text)]
        if norm and norm != text:
            vecs.append(self.embed_query(norm))
        return np.vstack(vecs)

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed([QUERY_PREFIX + text])[0]


# ---------------------------------------------------------------- 切块
def _hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _day(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:  # noqa: BLE001
        return ""


def chunks_from_archive() -> list[dict]:
    p = DATA / "archive.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    out, i = [], 0
    while i < len(rows):
        r = rows[i]
        if r.get("role") != "user":
            i += 1
            continue
        u = (r.get("text") or "").strip()
        a = ""
        if i + 1 < len(rows) and rows[i + 1].get("role") == "assistant":
            a = (rows[i + 1].get("text") or "").strip()
            i += 1
        i += 1
        # 去掉注入的时间戳和状态串，只留他真正说的话
        u = re.sub(r"（现在是[^）]*）", "", u)
        if "；你的心情" in u:
            # 状态串一直延续到最后（可能以《歌单》结束），取它后面那句
            if "》" in u:
                u = u.rsplit("》", 1)[-1]
            else:
                u = u.rsplit("；", 1)[-1]
        u = u.split("（最后一张图")[0].strip()
        a = re.sub(r"^\s+", "", a)
        if not u:
            i += 1
            continue
        text = ("他：" + u[:200]) + ("　你：" + a[:200] if a else "")
        text = text.strip()
        if len(text) < 8:
            continue
        out.append({"id": _hash(text), "type": "chat", "date": _day(r.get("ts", 0)), "text": text[:400]})
    return out


def chunks_from_diary() -> list[dict]:
    out = []
    seen = set()

    # 离线日记流水（全量）
    p = DATA / "diary_offline.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                o = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            t = (o.get("diary") or "").strip()
            if len(t) < 6 or t in seen:
                continue
            seen.add(t)
            out.append({"id": _hash(t), "type": "diary",
                        "date": str(o.get("date") or _day(o.get("ts", 0))), "text": t[:400]})

    # persona 里的日记（含 QQ 通道写的）
    try:
        d = json.loads((DATA / "persona.json").read_text(encoding="utf-8-sig"))
        for x in (d.get("diary") or []):
            t = (x.get("text") or "").strip()
            if len(t) < 6 or t in seen:
                continue
            seen.add(t)
            out.append({"id": _hash(t), "type": "diary",
                        "date": str(x.get("date") or _day(x.get("ts", 0))), "text": t[:400]})
        # 她写给他的回信摘要
        for x in (d.get("notes") or []):
            t = (x.get("text") or "").strip()
            if len(t) < 10 or t in seen:
                continue
            seen.add(t)
            out.append({"id": _hash(t), "type": "note",
                        "date": str(x.get("date") or ""), "text": t[:400]})
    except Exception:  # noqa: BLE001
        pass
    return out


def chunks_from_shared() -> list[dict]:
    out = []
    p = DATA / "shared.jsonl"
    if not p.exists():
        return []
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            o = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        t = (o.get("text") or "").strip()
        if len(t) < 4:
            continue
        out.append({"id": _hash(t), "type": "told",
                    "date": _day(o.get("ts", 0)), "text": t[:400]})
    return out


def chunks_from_letters() -> list[dict]:
    out = []
    for sub, typ in (("dad_letters", "dad_letter"), ("letters_to_dad", "her_letter")):
        d = DATA / sub
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            body = f.read_text(encoding="utf-8", errors="ignore")
            for para in body.split("\n\n"):
                t = para.strip()
                if len(t) < 15:
                    continue
                out.append({"id": _hash(t), "type": typ, "date": f.stem, "text": t[:400]})
    return out


def chunks_from_memory() -> list[dict]:
    """长期事实和情绪原话也入索引，这样"我说过我胃不好吗"这类问题能被想起。"""
    out = []
    try:
        m = json.loads((DATA / "memory.json").read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return out
    for key, rec in (m or {}).items():
        if not isinstance(rec, dict):
            continue
        for f in (rec.get("facts") or []):
            t = (f or "").strip()
            if len(t) >= 4:
                out.append({"id": _hash("fact:" + t), "type": "fact",
                            "date": "", "text": t[:400]})
        for mo in (rec.get("moments") or []):
            t = (mo.get("text") or "").strip()
            if len(t) >= 6:
                out.append({"id": _hash("moment:" + t), "type": "moment",
                            "date": _day(mo.get("ts", 0)), "text": t[:400]})
    return out


def chunks_from_persona() -> list[dict]:
    """她的口味、歌单等自我设定也入索引。"""
    out = []
    try:
        d = json.loads((DATA / "persona.json").read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return out
    prefs = d.get("prefs") or []
    if prefs:
        text = "她的口味：" + "、".join(prefs)
        out.append({"id": _hash("pref:" + text), "type": "pref", "date": "", "text": text})
    pl = (d.get("playlist") or "").strip()
    if pl:
        out.append({"id": _hash("playlist:" + pl), "type": "pref", "date": "",
                    "text": f"她在听《{pl}》"})
    return out


def all_chunks() -> list[dict]:
    items = (chunks_from_archive() + chunks_from_diary() + chunks_from_shared()
             + chunks_from_letters() + chunks_from_memory()
             + chunks_from_persona())
    seen, out = set(), []
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        out.append(it)
    return out


# ---------------------------------------------------------------- 索引
class VecIndex:
    def __init__(self):
        self.meta: list[dict] = []
        self.vectors: np.ndarray | None = None

    # -- 读写 --
    def load(self) -> bool:
        if not (VEC_PATH.exists() and META_PATH.exists()):
            return False
        try:
            self.vectors = np.load(VEC_PATH)
            self.meta = []
            for line in META_PATH.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.meta.append(json.loads(line))
            return len(self.meta) == len(self.vectors) > 0
        except Exception:  # noqa: BLE001
            self.vectors, self.meta = None, []
            return False

    def save(self):
        VEC_DIR.mkdir(parents=True, exist_ok=True)
        if self.vectors is not None:
            np.save(VEC_PATH, self.vectors)
        with META_PATH.open("w", encoding="utf-8") as f:
            for m in self.meta:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

    # -- 增量 --
    def add(self, items: list[dict]) -> int:
        if not items:
            return 0
        have = {m["id"] for m in self.meta}
        new = [x for x in items if x["id"] not in have]
        if not new:
            return 0
        vecs = Embedder.get().embed([x["text"] for x in new])
        self.vectors = vecs if self.vectors is None or len(self.vectors) == 0 else np.vstack([self.vectors, vecs])
        self.meta.extend(new)
        return len(new)

    def rebuild(self) -> int:
        items = all_chunks()
        self.meta, self.vectors = [], None
        n = self.add(items)
        self.save()
        return n

    def sync(self) -> int:
        if self.vectors is None and not self.load():
            return self.rebuild()
        n = self.add(all_chunks())
        if n:
            self.save()
        # 两层必须一起更新：只同步向量会让 SQLite 落后，检索范围对不上
        try:
            import memdb
            memdb.sync()
        except Exception:  # noqa: BLE001
            pass
        return n

    # -- 检索 --
    def search(self, query: str, k: int = 2, threshold: float = 0.45,
               exclude_ids: set[str] | None = None,
               allowed_ids: set[str] | None = None,
               prefer_ids: set[str] | None = None,
               prefer_bonus: float = 0.0) -> list[dict]:
        """两层检索：先用结构化条件圈定候选（allowed_ids），再按语义排序。

        allowed_ids=None 表示不限制；prefer_ids 里的记录加一点分（关键词命中）。
        """
        if self.vectors is None or len(self.meta) == 0:
            return []
        qs = Embedder.get().embed_queries(query)
        sims = (self.vectors @ qs.T).max(axis=1)   # 原句与归一化句取较好者

        # 第一层：结构化硬过滤（0 误差，只影响候选范围）
        if allowed_ids is not None:
            mask = np.array([m.get("id") in allowed_ids for m in self.meta])
            sims = np.where(mask, sims, -1.0)

        # 阈值只认语义分（base）；关键词加分只用于排序，不能把不合格的记录"顶"过阈值
        base = sims.copy()
        if prefer_ids:
            bonus = np.array([prefer_bonus if m.get("id") in prefer_ids else 0.0 for m in self.meta])
            sims = sims + bonus

        # 关键词兜底：字面命中够多时，允许语义分低一点也算相关
        kw_floor = 0.45
        kw_need = 2
        grams = {query[i:i + 2] for i in range(len(query) - 1)}
        grams = {g for g in grams if g.strip() and g not in _STOP_GRAMS}
        if grams:
            kw_hits = np.array([
                len([g for g in grams if g in (m.get("text") or "")]) for m in self.meta
            ])
            boost_mask = (kw_hits >= kw_need) & (base >= kw_floor) & (base < threshold)
            if boost_mask.any():
                sims = np.where(boost_mask, np.maximum(sims, threshold + 0.001), sims)

        order = np.argsort(-sims)
        out = []
        for idx in order:
            if len(out) >= k:
                break
            score = float(sims[idx])
            if float(base[idx]) < threshold:      # 用未加分的语义分做门槛
                continue
            m = dict(self.meta[idx])
            if exclude_ids and m["id"] in exclude_ids:
                continue
            m["score"] = round(score, 3)
            out.append(m)
        return out

    def status(self) -> dict:
        types: dict[str, int] = {}
        for m in self.meta:
            types[m.get("type", "?")] = types.get(m.get("type", "?"), 0) + 1
        return {"count": len(self.meta), "types": types,
                "vectors": 0 if self.vectors is None else int(self.vectors.shape[0]),
                "dim": 0 if self.vectors is None else int(self.vectors.shape[1])}


# ---------------------------------------------------------------- 对外
_INDEX: VecIndex | None = None


def get_index(cfg=None) -> VecIndex | None:
    """取全局索引；没建过或模型缺失就返回 None（调用方要能优雅降级）。"""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    try:
        if not (MODEL_DIR / "model_quantized.onnx").exists():
            return None
        idx = VecIndex()
        if not idx.load():
            return None
        _INDEX = idx
        return idx
    except Exception:  # noqa: BLE001
        return None


def recall(cfg, query: str, exclude_ids: set[str] | None = None) -> list[dict]:
    vcfg = (cfg.raw.get("vecmem") or {}) if cfg else {}
    if not vcfg.get("enable", True):
        return []
    idx = get_index(cfg)
    if idx is None:
        return []
    try:
        return idx.search(
            query,
            k=int(vcfg.get("topk", 2)),
            threshold=float(vcfg.get("threshold", 0.45)),
            exclude_ids=exclude_ids,
        )
    except Exception:  # noqa: BLE001
        return []


def format_block(hits: list[dict], max_chars: int = 150) -> str:
    if not hits:
        return ""
    # 来源标签：分清「他说过的」和「你自己写的」，避免把自己的话当成他的事实
    label = {"chat": "以前聊过", "diary": "你自己日记里写的",
             "note": "你自己写过的话", "told": "他转述的",
             "dad_letter": "他信里写的", "her_letter": "你自己回信里写的",
             "fact": "他让你记住的", "moment": "情绪原话", "pref": "你自己的设定"}
    try:
        import corrections as _corr
    except Exception:  # noqa: BLE001
        _corr = None
    lines = []
    for h in hits:
        t = (h.get("text") or "").replace("\n", " ").strip()
        if len(t) > max_chars:
            t = t[:max_chars] + "…"
        tip = ""
        if _corr is not None:
            m = _corr.match(h.get("text") or "")
            if m:
                tip = " " + m
        lines.append(f"- [{h.get('date', '')} {label.get(h.get('type'), '旧记录')}] {t}{tip}")
    return ("\n\n【你想起的旧事】（这些是过去的记录，不是他刚说的；"
            "只当成「我记得」，别当成事实，也别硬扯）\n" + "\n".join(lines))


# ---------------------------------------------------------------- CLI
def main():
    args = sys.argv[1:]
    idx = VecIndex()
    if "--rebuild" in args:
        t0 = time.time()
        n = idx.rebuild()
        print(f"重建完成：{n} 条，{time.time()-t0:.1f}s")
        print("状态:", idx.status())
        return 0
    if "--sync" in args:
        if not idx.load():
            print("还没建过索引，先 --rebuild")
            return 1
        n = idx.sync()
        print(f"增量完成：新增 {n} 条")
        print("状态:", idx.status())
        return 0
    if "--status" in args:
        if not idx.load():
            print("索引不存在")
            return 1
        print("状态:", idx.status())
        return 0
    if "--query" in args:
        q = args[args.index("--query") + 1]
        if not idx.load():
            print("索引不存在")
            return 1
        hits = idx.search(q, k=5, threshold=0.30)
        print(f"查询：{q}")
        for h in hits:
            print(f"  {h['score']:.3f}  [{h['date']} {h['type']}] {h['text'][:80]}")
        if not hits:
            print("  （没有足够相似的记录）")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
