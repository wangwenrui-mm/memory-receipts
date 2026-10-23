"""科研数据核验（lab 实例）—— 出厂版。

设计原则（出厂前验收标准）：
- 全本地、确定性、无 API 调用、无随机数；
- 抽取引擎覆盖：科学计数法、负数、范围（850–900）、千分位、百分比、
  单位别名（℃/°C、μm/um、cm²/cm-2/cm^-2）；
- 判定四态：有依据 / 矛盾 / 未溯源 / 计划；
- 自检与验收测试必须全部通过才算可用。

用法：
    python verify.py                       # 核验 lab/data/paper_draft.md
    python verify.py --selftest            # 内置边界用例
    python verify.py --acceptance          # 200 条基准 + 60 条标注陈述的验收
"""
# --- 输出编码保护（Windows 控制台默认 GBK，会让 emoji/中文崩溃）---
try:
    import sys as _sys
    for _s in (_sys.stdout, _sys.stderr):
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]          # lab/
DATA = BASE / "data"
RECORDS = DATA / "records.jsonl"
DRAFT = DATA / "paper_draft.md"
ACCEPTANCE = BASE / "tests" / "acceptance_cases.json"

# ---- 单位归一 ----
UNIT_MAP = {
    "℃": "degC", "°c": "degC", "°C": "degC", "度": "degC",
    "μm": "um", "um": "um", "微米": "um", "µm": "um", "Um": "um", "UM": "um",
    "nm": "nm", "纳米": "nm", "Nm": "nm", "NM": "nm",
    "cm²": "cm-2", "cm2": "cm-2", "cm^-2": "cm-2", "cm-2": "cm-2",
    "%": "%", "％": "%",
    "min": "min", "分钟": "min", "Min": "min",
    "s": "s", "秒": "s", "h": "h", "小时": "h",
    "v": "V", "V": "V", "mv": "mV", "mV": "mV",
    "ma": "mA", "mA": "mA", "a": "A", "A": "A",
    "pa": "Pa", "Pa": "Pa", "kpa": "kPa", "kPa": "kPa",
    "torr": "Torr", "Torr": "Torr",
    "k": "K", "K": "K", "ev": "eV", "eV": "eV", "kev": "keV", "keV": "keV",
    # 真实论文常见补充
    "T": "T", "mK": "mK", "GPa": "GPa", "MPa": "MPa", "kbar": "kbar",
    "g": "g", "mg": "mg", "W": "W", "mW": "mW", "sccm": "sccm",
    "cm-3": "cm-3", "cm−3": "cm-3", "cm^-3": "cm-3",
    "Ωcm": "ohm-cm", "Ω·cm": "ohm-cm", "ohm·cm": "ohm-cm",
    "at.%": "at%", "at%": "at%", "mol%": "mol%", "wt%": "wt%",
    "GHz": "GHz", "MHz": "MHz", "kHz": "kHz", "dB": "dB", "dBm": "dBm",
    "MeV": "MeV", "keV": "keV", "mT": "mT", "pL": "pL", "nL": "nL",
    "uL": "uL", "mL": "mL", "L": "L", "Hz": "Hz",
}
# 单位：已知表 + 复合单位兜底（W/mK、nm/min、eV-1cm-2）——进表即"未溯源"级别
UNIT_RE = ("|".join(re.escape(u) for u in sorted(UNIT_MAP, key=len, reverse=True))
           + r"|[A-Za-zμµ%°/²^\-]{1,8}(?:/[A-Za-z]{1,6})?")


def canon_unit(u: str) -> str:
    if u in UNIT_MAP:
        return UNIT_MAP[u]
    if "/" in u or "-" in u:
        return "composite:" + u
    return u


def unit_is_plausible(u_raw: str, u_canon: str) -> bool:
    """过滤"看起来像单位、其实不是"的噪声。

    拒绝：2D/3D 的 D、英文虚词（for/and）、材料名（C-SiC）。
    放行：表内单位、复合单位（W/mK、Ω·cm）、带数字的单位（eV-1cm-2）。
    """
    if u_raw in UNIT_MAP:
        return True
    if "/" in u_raw or "·" in u_raw:
        return True
    if any(ch.isdigit() for ch in u_raw):
        return True
    if u_raw.isalpha() and u_raw.islower():
        return False
    if len(u_raw) == 1:
        return False
    if "-" in u_raw:          # C-SiC 这类是材料名，不是单位
        return False
    return True


# 数字：可选负号 + 千分位 + 小数 + 科学计数法（3.2e18、3e19、-5、1,450、.5）
# 注意：指数形式必须能整体匹配，否则 "3e19" 会被拆成 3 + 单位"e"
NUM_RE = (r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?"
          r"|[+-]?\.\d+")

# 参数名：数字前面/单位后面最近的「中文或字母」段（放宽到 20 字，再按分隔符切段）
NAME_FRAG = r"[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9]{1,20}"

# 说明：
#   (?<![A-Za-z0-9])  名字不能从英文单词中间开始（sequence → equence）
#   (?<![A-Za-z0-9]-) 连字符编号不当数值（ESM-2、GPT-4、ResNet-50）
#   (?![A-Za-z])      单位后必须是词边界（"2 sequence" 不能把 s 当秒）
PAT = re.compile(
    rf"(?<![A-Za-z0-9])(?P<pre>{NAME_FRAG})[^0-9+−\-–—]{{0,6}}"
    rf"(?<![A-Za-z0-9]-)(?P<num>{NUM_RE})\s*(?P<unit>{UNIT_RE})(?![A-Za-z])"
)
RANGE_RE = re.compile(
    rf"(?<![A-Za-z0-9]-)(?P<num1>{NUM_RE})\s*(?:-|–|—|~|～|至|到)\s*"
    rf"(?P<num2>{NUM_RE})\s*(?P<unit>{UNIT_RE})(?![A-Za-z])"
)

PLAN_WORDS = ("计划", "拟", "准备", "打算", "后续", "下一步", "预计", "将要",
              "目标值", "设计值", "拟定", "规划")

# 通用参数「后缀词」：带这些后缀的名字，若前面的限定词与记录不符，视为不同参数
GENERIC_TAILS = ("温度", "厚度", "浓度", "迁移率", "时间", "电压", "电流",
                 "尺寸", "粗糙度", "速率", "宽度", "密度", "偏压", "气压",
                 "长度", "面积", "系数", "常数", "电阻率", "透过率", "反射率")


def _is_different_param(name: str, record_param: str) -> bool:
    """后缀相同但限定词不同 → 不同参数。

    比较「去掉通用后缀后的限定词」：
      钝化温度(钝化) vs 退火温度(退火) → 不同
      测试得到载流子迁移率(测试得到载流子) vs 载流子迁移率(载流子) → 相同（限定量是后缀）
    """
    for tail in GENERIC_TAILS:
        if name.endswith(tail) and len(name) > len(tail):
            head = name[: -len(tail)]
            if not record_param.endswith(tail):
                # 记录参数不带这个后缀（如"载流子迁移率"是完整名）→ 视为不同
                return True
            rec_head = record_param[: -len(tail)]
            if not rec_head:
                return False
            return rec_head not in head
    return False


def _sentence_of(text: str, start: int, end: int) -> str:
    s = max(text.rfind(ch, 0, start) for ch in "。！？\n") + 1
    ends = [x for x in (text.find(ch, end) for ch in "。！？\n") if x != -1]
    e = min(ends) if ends else len(text)
    return text[s:e]


def _to_num(raw: str) -> float:
    return float(raw.replace(",", ""))


def _param_cands(pre_raw: str, post: str) -> list[str]:
    """参数候选：就近优先、长串优先。

    例：「第111批样品的表面粗糙度为」→ 切段得 [第111批样品, 表面粗糙度]
        → 先取「表面粗糙度」（离数字最近的一段），再取它的子串
    """
    cands: list[str] = []

    def push(v: str):
        if len(v) >= 2 and v not in cands:
            cands.append(v)

    segs = [x for x in re.split(r"[的为=＝是：:,，、\s]+", pre_raw) if x]
    runs: list[str] = []
    for seg in segs:
        runs += re.findall(r"[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9]{0,12}", seg)
    if not runs:
        runs = re.findall(r"[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9]{0,12}", pre_raw)

    # 就近优先：离数字最近的段放前面
    for run in reversed(runs[-3:]):
        push(run)
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in run)
        if has_cjk:
            # 中文才切子串（氧化层厚度 → 厚度）；英文切了会产出 sequence→equence 这类碎片
            n = len(run)
            for length in range(n - 1, 1, -1):
                for i in range(0, n - length + 1):
                    push(run[i:i + length])
        if len(cands) >= 24:
            break

    # 单位之后的第一段（"在 850 ℃ 下退火" → 退火）
    m_post = re.match(r"[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9]{0,9}",
                      re.sub(r"^[下的为是，,、\s]+", "", post))
    if m_post:
        post_run = m_post.group(0)
        push(post_run)
        if any("\u4e00" <= ch <= "\u9fff" for ch in post_run):
            np_ = len(post_run)
            for length in range(np_ - 1, 1, -1):
                for i in range(0, np_ - length + 1):
                    push(post_run[i:i + length])
    return cands[:24]


POST_NAME = re.compile(
    rf"(?P<num>{NUM_RE})\s*(?P<unit>{UNIT_RE})\s*的(?P<name>{NAME_FRAG})"
)


def extract(text: str) -> list[dict]:
    # 「120±0.5 nm」→ 去掉容差部分，只核主值
    text = re.sub(r"±\s*\d+(?:\.\d+)?", "", text)
    claims = []
    for m in PAT.finditer(text):
        unit_raw = m.group("unit")
        u_canon = canon_unit(unit_raw)
        # 单字母 e/E 不是单位，是科学计数法指数的一部分（"7.263e+16"）
        if unit_raw in ("e", "E"):
            continue
        if not unit_is_plausible(unit_raw, u_canon):
            continue
        # 数值后面还紧跟数字（指数被截断）→ 丢弃这次匹配
        tail = text[m.end():m.end() + 1]
        if tail.isdigit():
            continue
        ctx = _sentence_of(text, m.start(), m.end())
        raw = m.group(0).strip()
        # 单位后的上下文只取到下一个标点，避免把下一条陈述的参数吸进来
        post_tail = re.split(r"[，,、。！？;；\n]", text[m.end():m.end() + 12])[0]
        cands = _param_cands(m.group("pre"), post_tail)
        claims.append({
            "kind": "point", "raw": raw,
            "param_cands": cands,
            "value": _to_num(m.group("num")),
            "unit": canon_unit(m.group("unit")),
            "unit_raw": m.group("unit"),
            "context": ctx,
        })
    for m in POST_NAME.finditer(text):
        unit_raw = m.group("unit")
        u_canon = canon_unit(unit_raw)
        if unit_raw in ("e", "E") or not unit_is_plausible(unit_raw, u_canon):
            continue
        name = m.group("name")
        cands = []
        n = len(name)
        for length in range(n, 1, -1):
            for i in range(0, n - length + 1):
                v = name[i:i + length]
                if len(v) >= 2 and v not in cands:
                    cands.append(v)
        claims.append({
            "kind": "point", "raw": m.group(0).strip(),
            "param_cands": cands, "value": _to_num(m.group("num")),
            "unit": u_canon, "unit_raw": unit_raw,
            "context": _sentence_of(text, m.start(), m.end()),
        })

    for m in RANGE_RE.finditer(text):
        if not unit_is_plausible(m.group("unit"), canon_unit(m.group("unit"))):
            continue
        ctx = _sentence_of(text, m.start(), m.end())
        claims.append({
            "kind": "range", "raw": m.group(0).strip(),
            "param_cands": _param_cands(
                text[max(0, m.start() - 14):m.start()], text[m.end():m.end() + 8]),
            "lo": _to_num(m.group("num1")), "hi": _to_num(m.group("num2")),
            "unit": canon_unit(m.group("unit")),
            "unit_raw": m.group("unit"),
            "context": ctx,
        })
    return claims


def load_records() -> list[dict]:
    if not RECORDS.exists():
        return []
    out = []
    for ln in RECORDS.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def _build_index(records: list[dict]):
    """倒排索引：param → 记录；子串(len2..12) → param 集合。"""
    by_param: dict[str, list[dict]] = {}
    for r in records:
        by_param.setdefault(r.get("param", ""), []).append(r)
    sub_idx: dict[str, set[str]] = {}
    for p in by_param:
        n = len(p)
        for length in range(2, min(12, n) + 1):
            for i in range(0, n - length + 1):
                sub_idx.setdefault(p[i:i + length], set()).add(p)
    return by_param, sub_idx


def verify(claims: list[dict], records: list[dict]) -> list[dict]:
    by_param, sub_idx = _build_index(records)
    return _verify(claims, by_param, sub_idx)


def _match_params(names: list[str], by_param, sub_idx,
                  max_ambiguous: int = 8) -> tuple[list[str], str]:
    """返回 (命中的参数名列表, 层级说明)。strong 优先，其次 loose。

    带具体限定词的参数名（钝化温度）若与某记录参数不符，则该记录不参与匹配；
    但不能因为"存在别的记录不符"就把这个名字整条丢掉。
    """
    specs = [n for n in names
             if any(n.endswith(t) and len(n) > len(t) for t in GENERIC_TAILS)]

    def blocked(p: str) -> bool:
        return bool(specs) and all(_is_different_param(n, p) for n in specs)

    strong: set[str] = set()
    for cand in names:
        for p in sub_idx.get(cand, ()):
            if blocked(p):
                continue
            if cand == p or len(cand) >= 3:
                strong.add(p)
    if strong:
        return sorted(strong), "strong"
    loose: set[str] = set()
    for cand in names:
        # 名字是参数的子串（含 2 字通用词，如 厚度/温度）
        loose |= {p for p in sub_idx.get(cand, ()) if not blocked(p)}
        n = len(cand)
        for length in range(n, 1, -1):
            for i in range(0, n - length + 1):
                p = cand[i:i + length]
                if p in by_param and not blocked(p):
                    loose.add(p)
    # 名字超长（>12 字）时子串索引兜不住，退回有限扫描
    if not loose:
        for p in by_param:
            if blocked(p):
                continue
            if any(c and (c == p or c in p or p in c) for c in names):
                loose.add(p)
    if len(loose) > max_ambiguous:
        return [], f"ambiguous({len(loose)})"
    return sorted(loose), "loose"


def _verify(claims: list[dict], by_param, sub_idx) -> list[dict]:
    results = []
    for c in claims:
        ctx = c.get("context") or ""
        if any(w in ctx for w in PLAN_WORDS):
            results.append({**c, "state": "计划", "why": "规划/目标值，不作数据核验"})
            continue

        names = c.get("param_cands") or []
        params, tier = _match_params(names, by_param, sub_idx)
        if tier.startswith("ambiguous"):
            results.append({**c, "state": "未溯源",
                            "why": f"参数歧义：同时命中 {tier[10:-1]} 个参数，无法定位"})
            continue
        if not params:
            results.append({**c, "state": "未溯源", "why": "原始记录中没有这个参数"})
            continue

        recs = [r for p in params for r in by_param[p]]
        unit = c.get("unit")
        same = [r for r in recs if canon_unit(r.get("unit", "")) == unit]
        if not same:
            got_units = sorted({canon_unit(r.get("unit", "")) for r in recs})
            results.append({**c, "state": "未溯源",
                            "why": f"参数存在但单位不符（记录单位 {got_units[:3]}）"})
            continue

        cur, hist = _split_current(same)

        def _match(recs):
            vals = [float(r["value"]) for r in recs]
            if c["kind"] == "range":
                hit = [v for v in vals if c["lo"] <= v <= c["hi"]]
            else:
                hit = [v for v in vals if abs(v - c["value"]) < 1e-9]
            return hit

        hit = _match(cur)
        if hit:
            src = _src_of(cur, hit)
            warn = ""
            cur_vals = {float(r["value"]) for r in cur}
            if len(cur_vals) > 1:
                warn = f"（注意：当前记录存在 {len(cur_vals)} 个不同取值 {sorted(cur_vals)[:3]}，需人工确认）"
            results.append({**c, "state": "有依据", "why": f"当前记录取值 {hit[:3]}{src}{warn}"})
            continue

        hit_old = _match(hist)
        if hit_old:
            detail = _fresh_detail(cur)
            results.append({**c, "state": "已过时",
                            "why": f"该值来自旧记录（{hit_old[:2]}），已被更新为 {detail}"})
            continue

        vals = [float(r["value"]) for r in same]
        results.append({**c, "state": "矛盾",
                        "why": f"记录取值 {vals[:3]}，与陈述不一致"})
    return results


_DATE_PATTERNS = [
    (r"^(\d{4})-(\d{1,2})-(\d{1,2})", "{0}-{1:0>2}-{2:0>2}"),
    (r"^(\d{4})/(\d{1,2})/(\d{1,2})", "{0}-{1:0>2}-{2:0>2}"),
    (r"^(\d{4})\.(\d{1,2})\.(\d{1,2})", "{0}-{1:0>2}-{2:0>2}"),
    (r"^(\d{4})年(\d{1,2})月(\d{1,2})日", "{0}-{1:0>2}-{2:0>2}"),
    (r"^(\d{4})(\d{2})(\d{2})$", "{0}-{1}-{2}"),
]


def _norm_date(raw) -> str:
    """把常见日期写法归一为 YYYY-MM-DD；无法识别返回空串（视为无日期）。"""
    t = str(raw or "").strip()
    if not t:
        return ""
    for pat, fmt in _DATE_PATTERNS:
        m = re.match(pat, t)
        if m:
            try:
                return fmt.format(m.group(1), int(m.group(2)), int(m.group(3)))
            except Exception:
                return ""
    return ""


def _norm_scope(raw) -> str:
    """样本标识归一：去空白、统一大小写。"""
    return re.sub(r"\s+", "", str(raw or "")).casefold()


def _date_of(r: dict) -> str:
    return _norm_date(r.get("date"))


def _split_current(recs: list[dict]) -> tuple[list[dict], list[dict]]:
    """区分「当前值」与「历史值」。

    只有当覆盖关系被**显式声明**时才作废旧值，避免把不同样品的合法数据当成过时：
      1) 记录带 superseded_by 字段 → 直接判为历史值；
      2) 记录声明了 scope（样本/批次），且同 (参数, scope) 内存在更晚日期的记录
         → 较晚者为当前值，较早者为历史值；
      3) 未声明 scope 且无 superseded_by → 一律视为当前值（不隐式作废）。
    """
    cur, hist = [], []
    remaining = []
    for r in recs:
        if r.get("superseded_by"):
            hist.append(r)
        else:
            remaining.append(r)

    groups: dict[tuple, list[dict]] = {}
    for r in remaining:
        groups.setdefault((r.get("param"), _norm_scope(r.get("scope"))), []).append(r)

    for key, grp in groups.items():
        scope = key[1]
        if not scope:
            cur.extend(grp)
            continue
        latest = max(_date_of(r) for r in grp)
        for r in grp:
            (cur if _date_of(r) == latest else hist).append(r)
    return cur, hist


def _src_of(recs: list[dict], vals: list[float]) -> str:
    for r in recs:
        try:
            if float(r["value"]) in vals:
                src = r.get("source") or ""
                dt = r.get("date") or ""
                tag = "，".join(x for x in (src, dt) if x)
                return f"（来源 {tag}）" if tag else ""
        except Exception:
            continue
    return ""


def _fresh_detail(cur: list[dict]) -> str:
    if not cur:
        return "（无更新记录）"
    r = cur[0]
    parts = [str(r.get("value")), str(r.get("unit") or "")]
    src = r.get("source") or ""
    dt = r.get("date") or ""
    tag = "，".join(x for x in (src, dt) if x)
    return " ".join(parts).strip() + (f"（{tag}）" if tag else "")


MARK = {"有依据": "✅", "未溯源": "⚠️", "矛盾": "❌", "计划": "○", "已过时": "⏳"}


def report(results: list[dict], title: str = "") -> int:
    if title:
        print(f"\n=== {title} ===")
    total = len(results)
    bad = [r for r in results if r["state"] not in ("有依据", "计划", "已过时")]
    stale = [r for r in results if r["state"] == "已过时"]
    print(f"抽取陈述 {total} 条；未溯源/矛盾 {len(bad)} 条；已过时 {len(stale)} 条")
    for r in results:
        print(f"  {MARK[r['state']]} [{r['state']}] {r['raw'][:44]:46s} {r['why']}")
    return len(bad)


# ---------------------------------------------------------------- 自检
SELFTEST_CASES = [
    ("样品在 850 ℃ 下退火", "有依据"),
    ("退火温度升到 900 ℃", "矛盾"),
    ("氧化层厚度为 120 nm", "有依据"),
    ("栅极电压设为 5 V", "未溯源"),
    ("载流子浓度 3.2e18 cm-2", "有依据"),      # 科学计数法
    ("温度降到 -5 ℃", "矛盾"),                 # 负数
    ("退火温度 850–900 ℃", "有依据"),          # 范围：850 在范围内
    ("退火温度 200–300 ℃", "矛盾"),            # 范围：850 不在范围内
    ("掺杂浓度 1,450 cm-2", "未溯源"),         # 千分位；但「掺杂浓度」≠「载流子浓度」
    ("退火温度 850 °C", "有依据"),             # 单位别名
    ("氧化层厚度 120 μm", "未溯源"),           # 单位不一致
    ("后续把退火温度提高到 900 ℃", "计划"),     # 计划语
    ("薄膜厚度为 120 nm", "未溯源"),           # 「薄膜」限定词≠「氧化层」→ 不同参数
    ("厚度为 120 nm", "有依据"),               # 只用通用后缀，无具体限定词 → 可绑定
    ("浓度为 3.2e18 cm-2", "有依据"),          # 通用后缀 + 数值命中
]


def selftest() -> int:
    recs = [
        {"param": "退火温度", "value": 850, "unit": "℃"},
        {"param": "氧化层厚度", "value": 120, "unit": "nm"},
        {"param": "载流子迁移率", "value": 1450, "unit": "cm²"},
        {"param": "载流子浓度", "value": 3.2e18, "unit": "cm-2"},
    ]
    ok = 0
    print("=== 自检（边界用例）===")
    for text, expect in SELFTEST_CASES:
        res = verify(extract(text), recs)
        got = res[0]["state"] if res else "无陈述"
        good = got == expect
        ok += good
        print(f"  {'✅' if good else '❌'} 「{text}」→ {got}（期望 {expect}）"
              + ("" if good else f"  why={res[0]['why'] if res else ''}"))
    print(f"自检通过 {ok}/{len(SELFTEST_CASES)}")
    return 0 if ok == len(SELFTEST_CASES) else 1


# ---------------------------------------------------------------- 验收
def acceptance() -> int:
    if not ACCEPTANCE.exists():
        print("缺少验收用例：", ACCEPTANCE)
        return 1
    spec = json.loads(ACCEPTANCE.read_text(encoding="utf-8"))
    records = spec.get("records", [])
    cases = spec.get("cases", [])
    if len(records) != 200:
        print(f"警告：基准记录 {len(records)} 条（应为 200）")
    tp = fp = fn = tn = 0
    wrong = []
    for case in cases:
        res = verify(extract(case["text"]), records)
        got = res[0]["state"] if res else "无陈述"
        expect = case["expect"]
        if expect == "有依据":
            if got == "有依据":
                tp += 1
            else:
                fn += 1
                wrong.append((case["text"], got, expect, res[0]["why"] if res else ""))
        else:
            if got == expect:
                tn += 1
            else:
                fp += 1
                wrong.append((case["text"], got, expect, res[0]["why"] if res else ""))
    n = len(cases)
    print(f"=== 验收测试：{len(records)} 条记录 × {n} 条标注陈述 ===")
    print(f"  通过 {tp + tn}/{n}　误判 {fp + fn}")
    for text, got, expect, why in wrong:
        print(f"  ❌ 「{text[:40]}」→ {got}（期望 {expect}）{why}")
    return 0 if not wrong else 1


def _version() -> str:
    try:
        vf = Path(__file__).resolve().parents[2] / "VERSION"
        return vf.read_text(encoding="utf-8").strip()
    except Exception:
        return "unknown"


def main():
    if "--version" in sys.argv:
        print("lab-verify", _version())
        return 0
    if "--selftest" in sys.argv:
        return selftest()
    if "--acceptance" in sys.argv:
        return acceptance()
    records = load_records()
    if not DRAFT.exists():
        print("缺少报告草稿：", DRAFT)
        return 1
    text = DRAFT.read_text(encoding="utf-8")
    print(f"原始记录 {len(records)} 条；报告草稿 {len(text)} 字")
    bad = report(verify(extract(text), records), "核验结果")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
