"""轻量语言识别：zh / en / id（其他返回 other）。

规则可解释：CJK 字符比例 → 中文；拉丁文本按英语/印尼语高频功能词得分区分。
"""
from __future__ import annotations

import re

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_WORD = re.compile(r"[A-Za-z]+")

_EN = set("""the of and to in is are was were for on with that this by as be from at or an it its
which have has not will their our we been than these other also more such all can may into""".split())
_ID = set("""yang dan di ke dari untuk dengan pada ini itu dalam adalah tidak akan oleh kami kita
atau juga telah sebagai tersebut serta bagi setiap lebih para secara karena dapat sudah harus perusahaan
pengelolaan limbah air emisi karyawan tahun""".split())

LANG_NAMES = {"zh": "中文", "en": "English", "id": "Bahasa Indonesia", "other": "其他", "mixed": "混合"}


def detect(text: str) -> str:
    if not text:
        return "other"
    sample = text[:4000]
    cjk = len(_CJK.findall(sample))
    words = [w.lower() for w in _WORD.findall(sample)]
    letters = sum(len(w) for w in words)
    if cjk and cjk >= letters * 0.3:
        return "zh"
    if not words:
        return "zh" if cjk else "other"
    en = sum(1 for w in words if w in _EN)
    idn = sum(1 for w in words if w in _ID)
    if en == 0 and idn == 0:
        return "en" if letters > 0 else "other"
    return "id" if idn > en else "en"


def detect_many(texts: list[str]) -> str:
    counts: dict[str, int] = {}
    for t in texts:
        lang = detect(t)
        counts[lang] = counts.get(lang, 0) + len(t)
    if not counts:
        return "other"
    return max(counts.items(), key=lambda kv: kv[1])[0]
