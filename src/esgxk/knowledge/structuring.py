"""知识结构化（LOCAL_KNOWLEDGE_STANDARD）：由 Chunk 生成知识条目。

确定性、可解释：按章节/表格/幻灯片聚合 → 标题、摘要（首句）、关键词（词频）、ESG 主题（三语词典）。
不调用大模型，因此不会引入无法追溯的内容；每个条目保留 chunk_id 与 Locator。
"""
from __future__ import annotations

import re
from collections import Counter

ESG_TOPICS: dict[str, list[str]] = {
    "E-气候与排放": ["温室气体", "排放", "碳", "二氧化碳", "范围一", "范围二", "范围三", "greenhouse", "emission",
                   "carbon", "co2", "scope 1", "scope 2", "scope 3", "climate", "emisi", "karbon", "gas rumah kaca",
                   "iklim"],
    "E-能源": ["能源", "电力", "用电", "光伏", "可再生", "energy", "electricity", "renewable", "solar", "energi",
              "listrik", "terbarukan"],
    "E-水资源": ["水资源", "取水", "用水", "废水", "污水", "water", "wastewater", "air limbah", "penggunaan air"],
    "E-废弃物": ["废弃物", "固废", "危险废物", "回收", "waste", "hazardous", "recycl", "limbah", "daur ulang", "b3"],
    "E-生物多样性": ["生物多样性", "生态", "biodiversity", "ecosystem", "keanekaragaman hayati"],
    "S-职业健康安全": ["安全", "职业健康", "工伤", "事故", "safety", "injury", "incident", "ltifr", "keselamatan",
                   "kesehatan kerja", "kecelakaan"],
    "S-员工": ["员工", "培训", "多元化", "薪酬", "雇员", "employee", "training", "diversity", "workforce", "karyawan",
             "pelatihan", "tenaga kerja"],
    "S-社区与人权": ["社区", "公益", "人权", "童工", "community", "human rights", "child labor", "masyarakat",
                "hak asasi"],
    "G-公司治理": ["治理", "董事会", "合规", "反腐", "反贿赂", "风险管理", "governance", "board", "compliance",
               "anti-corruption", "bribery", "risk management", "tata kelola", "dewan", "kepatuhan", "korupsi"],
    "G-供应链": ["供应链", "供应商", "采购", "supply chain", "supplier", "procurement", "rantai pasok", "pemasok",
              "pengadaan"],
}

_STOP = set("""的 了 和 与 及 在 是 为 对 等 中 年 我们 公司 进行 以及 其 该 各 并 将 已 由 于 也 或
the of and to in is are was were for on with that this by as be from at or an it its we our have has
yang dan di ke dari untuk dengan pada ini itu dalam adalah akan oleh kami atau juga telah""".split())
_WORD = re.compile(r"[A-Za-z][A-Za-z\-]{2,}|[一-鿿]{2,4}")


def classify_topic(text: str) -> str | None:
    low = text.lower()
    best, score = None, 0
    for topic, words in ESG_TOPICS.items():
        s = sum(low.count(w) for w in words)
        if s > score:
            best, score = topic, s
    return best


def keywords(text: str, k: int = 8) -> list[str]:
    counts = Counter(w.lower() for w in _WORD.findall(text) if w.lower() not in _STOP)
    return [w for w, _ in counts.most_common(k)]


def first_sentence(text: str, limit: int = 220) -> str:
    body = text.strip().replace("\n", " ")
    m = re.search(r"^(.{20,}?[。！？.!?])(\s|$)", body)
    s = m.group(1) if m else body
    return s[:limit] + ("…" if len(s) > limit else "")


def build_items(chunks: list[dict]) -> list[dict]:
    """chunks：同一版本的 Chunk 字典列表（chunk_id, chunk_type, text, heading, locator, language）。"""
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for c in chunks:
        if c.get("chunk_type") in ("TABLE", "NOTES"):
            key = ("single", c["chunk_id"])
        else:
            key = ("section", c.get("heading") or "", c.get("slide"), c.get("sheet"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(c)
    items = []
    for key in order:
        members = groups[key]
        head = members[0]
        text = "\n".join(m["text"] for m in members)
        kind = "TABLE" if head.get("chunk_type") == "TABLE" else ("NOTES" if head.get("chunk_type") == "NOTES" else "SECTION")
        title = head.get("heading") or first_sentence(head["text"], 60)
        body = text
        if head.get("heading") and body.startswith(head["heading"]):
            body = body[len(head["heading"]):].strip()
        items.append({
            "chunk_id": head["chunk_id"],
            "kind": kind,
            "title": title[:120],
            "summary": first_sentence(body or text),
            "keywords": ", ".join(keywords(text)),
            "esg_topic": classify_topic(text),
            "language": head.get("language"),
            "locator": head["locator"],
        })
    return items
