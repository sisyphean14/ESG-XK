"""FTS 文本规范化。

SQLite FTS5 unicode61 分词器把连续的 CJK 字符视为一个 token，无法做中文子串检索。
ESG-XK 在索引与查询两端都把 CJK 字符按单字切分（字之间插入空格），中文查询转为短语查询，
从而实现任意长度中文子串的精确匹配；拉丁语系文本保持按词、去重音匹配。
"""
from __future__ import annotations

import re
import unicodedata

_CJK_CHAR = r"぀-ヿ㐀-䶿一-鿿豈-﫿가-힯"
_CJK_RE = re.compile(f"([{_CJK_CHAR}])")
_TOKEN_RE = re.compile(f"[{_CJK_CHAR}]+|[0-9A-Za-zÀ-ɏ]+(?:[.,][0-9]+)*")
_SPACES = re.compile(r"\s+")


def for_index(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    return _SPACES.sub(" ", _CJK_RE.sub(r" \1 ", text)).strip()


def is_cjk(token: str) -> bool:
    return bool(re.fullmatch(f"[{_CJK_CHAR}]+", token))


def query_terms(query: str) -> list[str]:
    """把用户查询拆为检索词（中文连续串为一个词，拉丁词为一个词）。"""
    q = unicodedata.normalize("NFKC", query or "")
    return [m.group(0) for m in _TOKEN_RE.finditer(q)]


def _quote(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def _term_to_fts(term: str) -> str:
    # 数字中的逗号/小数点在 unicode61 中是分隔符 → 转短语
    if is_cjk(term):
        return _quote(" ".join(term))
    parts = re.split(r"[.,]", term)
    parts = [p for p in parts if p]
    if len(parts) > 1:
        return _quote(" ".join(parts))
    return _quote(term)


def build_match(query: str, *, mode: str = "all", columns: tuple[str, ...] | None = None) -> str | None:
    """构建 FTS5 MATCH 表达式。

    mode: all = 所有词都出现（AND）；any = 任一词（OR）；phrase = 整体短语。
    支持用户使用双引号标记短语，例如：  "范围一 排放" 废水
    """
    if not query or not query.strip():
        return None
    q = unicodedata.normalize("NFKC", query.strip())
    parts: list[str] = []
    if mode == "phrase":
        terms = query_terms(q)
        if not terms:
            return None
        tokens: list[str] = []
        for t in terms:
            tokens.extend(list(t) if is_cjk(t) else [p for p in re.split(r"[.,]", t) if p])
        parts = [_quote(" ".join(tokens))]
    else:
        for m in re.finditer(r'"([^"]+)"|(\S+)', q):
            if m.group(1):
                terms = query_terms(m.group(1))
                tokens = []
                for t in terms:
                    tokens.extend(list(t) if is_cjk(t) else [p for p in re.split(r"[.,]", t) if p])
                if tokens:
                    parts.append(_quote(" ".join(tokens)))
            else:
                for t in query_terms(m.group(2)):
                    parts.append(_term_to_fts(t))
    if not parts:
        return None
    joiner = " OR " if mode == "any" else " AND "
    expr = joiner.join(parts)
    if columns:
        expr = "{" + " ".join(columns) + "} : (" + expr + ")"
    return expr


_Q_STOP_ZH = "的了吗呢吧啊是在有和与及或等哪些什么怎么如何多少是否请问一下关于对于为何为什么情况"
_Q_STOP_EN = set("""what which who whom how many much is are was were the a an of in on for to and or do does did
with by at from about please tell me our their its this that these those be been any there""".split())
_Q_STOP_ID = set("""apa berapa bagaimana siapa yang dan di ke dari untuk dengan pada ini itu adalah apakah
tentang tolong jelaskan""".split())


def natural_terms(query: str) -> list[str]:
    """自然语言问题 → 检索词：中文按二元组切分（去除疑问/虚词），拉丁词去停用词。"""
    out: list[str] = []
    for t in query_terms(query):
        if is_cjk(t):
            cleaned = "".join(ch if ch not in _Q_STOP_ZH else " " for ch in t)
            for seg in cleaned.split():
                if len(seg) == 1:
                    continue
                out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
        else:
            lw = t.lower()
            if lw in _Q_STOP_EN or lw in _Q_STOP_ID or (len(lw) < 2 and not lw.isdigit()):
                continue
            out.append(t)
    seen, uniq = set(), []
    for t in out:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    return uniq


def build_natural_match(query: str) -> str | None:
    terms = natural_terms(query)
    if not terms:
        return None
    return " OR ".join(_term_to_fts(t) for t in terms)


def highlight_terms(query: str) -> list[str]:
    terms = []
    for m in re.finditer(r'"([^"]+)"|(\S+)', unicodedata.normalize("NFKC", query or "")):
        chunk = m.group(1) or m.group(2)
        terms.extend(query_terms(chunk))
    return sorted(set(t for t in terms if t), key=len, reverse=True)


def make_snippet(text: str, terms: list[str], width: int = 220) -> tuple[str, list[tuple[int, int]]]:
    """返回 (片段, 高亮区间列表)，区间基于片段文本。"""
    if not text:
        return "", []
    low = text.lower()
    first = None
    for t in terms:
        i = low.find(t.lower())
        if i >= 0 and (first is None or i < first):
            first = i
    start = 0 if first is None else max(0, first - width // 3)
    end = min(len(text), start + width)
    snippet = text[start:end].replace("\n", " ")
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    snippet = prefix + snippet + suffix
    spans: list[tuple[int, int]] = []
    s_low = snippet.lower()
    for t in terms:
        tl = t.lower()
        pos = 0
        while tl and (i := s_low.find(tl, pos)) >= 0:
            spans.append((i, i + len(tl)))
            pos = i + len(tl)
    spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    return snippet, merged
