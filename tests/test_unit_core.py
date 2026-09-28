"""Unit：错误码、语言识别、文本规范化、Locator、分块、知识结构化、资源 Profile。"""
from esgxk.chunking.chunker import chunk_units
from esgxk.core.errors import MESSAGES, ErrorCode, EsgError, describe
from esgxk.core.langdetect import detect
from esgxk.index import textnorm
from esgxk.knowledge.structuring import build_items, classify_topic
from esgxk.parsers.base import locator_text, split_paragraphs


def test_every_error_code_has_message():
    codes = [v for k, v in vars(ErrorCode).items() if k.isupper()]
    assert len(codes) > 60
    for c in codes:
        assert c in MESSAGES and describe(c) != c
    prefixes = {c.split("_")[0] for c in codes}
    for p in ("IMPORT", "PARSE", "OCR", "INDEX", "VECTOR", "RAG", "ASSET", "RESOURCE", "DB", "BACKUP", "RESTORE",
              "PROFILE", "RUN", "ESTOP"):
        assert p in prefixes
    e = EsgError(ErrorCode.PARSE_CORRUPTED, "x")
    assert "[PARSE_CORRUPTED]" in e.user_message() and "未知" not in e.user_message()


def test_language_detection():
    assert detect("华东制造基地2025年范围一温室气体排放量为12,345吨") == "zh"
    assert detect("Total water withdrawal decreased by 8.5% compared with the baseline.") == "en"
    assert detect("Limbah B3 dikelola oleh pihak ketiga yang berizin sesuai peraturan pemerintah.") == "id"


def test_fts_normalization_and_query():
    assert textnorm.for_index("范围一排放 Scope") == "范 围 一 排 放 Scope"
    assert textnorm.build_match("范围一") == '"范 围 一"'
    assert textnorm.build_match("12,345") == '"12 345"'
    assert " OR " in textnorm.build_match("a b", mode="any")
    assert textnorm.build_match('"范围 一" 排放').count('"') == 4
    nat = textnorm.natural_terms("2025年范围一排放是多少？")
    assert "范围" in nat and "排放" in nat and "多少" not in nat
    snip, spans = textnorm.make_snippet("前文 排放量为12345吨 后文", ["排放"])
    assert spans and snip[spans[0][0]:spans[0][1]] == "排放"


def test_locator_text():
    assert locator_text({"type": "pdf", "page": 3, "paragraph": 2}) == "第3页 · 第2段"
    assert "工作表「排放」" in locator_text({"type": "xlsx", "sheet": "排放", "row_start": 2, "row_end": 9})
    assert "第4张幻灯片" in locator_text('{"type": "pptx", "slide": 4}')


def test_split_paragraphs_merges_lines():
    paras = split_paragraphs("第一行内容，\n第二行内容。\n\n1.2 标题\n新段落。")
    assert paras[0] == "第一行内容，第二行内容。"
    assert "1.2 标题" in paras


def _u(i, t, **kw):
    d = {"unit_id": i, "unit_type": kw.pop("unit_type", "paragraph"), "text": t, "locator_obj": {"type": "pdf", **kw}}
    d.update(kw)
    return d


def test_chunker_respects_page_and_table_boundaries():
    units = [_u(1, "1 环境", unit_type="heading", page=1), _u(2, "甲" * 300, page=1, paragraph_index=1),
             _u(3, "乙" * 300, page=1, paragraph_index=2), _u(4, "丙" * 300, page=2, paragraph_index=1),
             _u(5, "表头\n行1\n行2", unit_type="table", page=2)]
    chunks = chunk_units(units, target_chars=700, max_chars=1100)
    assert [c.page for c in chunks] == [1, 2, 2]
    assert chunks[0].text.startswith("1 环境") and chunks[0].locator["paragraph_end"] == 2
    assert chunks[2].chunk_type == "TABLE"
    long = chunk_units([_u(1, "句子。" * 800, page=1)], 700, 1100)
    assert all(c.char_count <= 1100 for c in long) and len(long) > 1


def test_knowledge_structuring():
    assert classify_topic("范围一温室气体排放 12,345 吨") == "E-气候与排放"
    assert classify_topic("Tingkat kecelakaan kerja keselamatan") == "S-职业健康安全"
    items = build_items([{"chunk_id": 1, "chunk_type": "TEXT", "text": "1 环境\n排放下降了。第二句。", "heading": "1 环境",
                          "locator": "{}", "language": "zh", "slide": None, "sheet": None}])
    assert items[0]["title"] == "1 环境" and items[0]["esg_topic"]


def test_resource_profile_default():
    from esgxk.ai.resources import get_profile
    p = get_profile(None)
    assert p.name == "LOW_MEMORY_16G" and p.heavy_slots == 1
