"""Integration：完整 Pipeline（真实 Worker 进程 + 真实 PP-OCR + 真实 llama.cpp）→ OCR / Chunk / FTS / Vector /
Hybrid / Reranker / RAG / Citation / 治理范围 / 降级。"""
import json
import os

import pytest

from conftest import CORPUS, process_all
from esgxk.index import fts
from esgxk.index.fts import SearchFilters
from esgxk.parsers.base import locator_text


@pytest.fixture(scope="module")
def pctx(tmp_path_factory):
    from esgxk.core.profile import ProfileManager
    from esgxk.services.context import AppContext, init_new_data_root
    base = tmp_path_factory.mktemp("pipe")
    os.environ["ESGXK_PROFILE_DIR"] = str(base / "profile")
    dr = init_new_data_root(base / "data")
    c = AppContext(ProfileManager(base / "profile"))
    c.create_profile(str(dr.root))
    c.open()
    caps = c.startup_report.capabilities
    if not all(caps[k]["available"] for k in ("ocr", "embedding", "reranker", "llm")):
        pytest.skip(f"本机 AI 资产不完整：{caps}")
    c.imports.quick_import(c.conn, "FOLDER", [str(CORPUS)], register_source=True)
    rid = process_all(c, "LOCAL_KNOWLEDGE_STANDARD", timeout=1800)
    c._rid = rid
    yield c
    c.close()
    os.environ.pop("ESGXK_PROFILE_DIR", None)


def q1(c, sql, *a):
    return c.conn.execute(sql, a).fetchone()


def test_run_completed_with_failures_isolated(pctx):
    r = pctx.runs.get_run(pctx._rid)
    # V1.1.0：队列全部走完但存在失败项 → COMPLETED_WITH_ERRORS（V1.0.0 为 COMPLETED）
    assert r["status"] == "COMPLETED_WITH_ERRORS"
    assert r["failed"] == 4                      # 3 个损坏文件 + 空文件，均不影响其他文档
    assert r["succeeded"] == r["total"] - 4
    errs = {x[0] for x in pctx.conn.execute("SELECT last_error_code FROM document_versions WHERE process_status='FAILED'")}
    assert errs == {"PARSE_CORRUPTED", "PARSE_EMPTY_FILE"}


def test_ocr_three_languages(pctx):
    def ocr_text(name):
        return "\n".join(r[0] or "" for r in pctx.conn.execute(
            "SELECT o.raw_text FROM ocr_results o JOIN document_versions v ON v.version_id=o.version_id "
            "WHERE v.file_name=? AND o.is_current=1", (name,)))
    zh = ocr_text("扫描件_环境管理制度.pdf")
    assert "温室气体" in zh and "危险废物" in zh
    assert "child labour" in ocr_text("Scanned_Supplier_Code.pdf").lower() or "child" in ocr_text("Scanned_Supplier_Code.pdf")
    idn = ocr_text("Pindaian_Kebijakan_K3.pdf")
    assert "karyawan" in idn.lower() and "pelindung" in idn.lower()
    assert "12" in ocr_text("光伏装机.png") and "光伏" in ocr_text("光伏装机.png")
    assert "mangrove" in ocr_text("mangrove_id.webp").lower()
    row = q1(pctx, "SELECT engine, languages, confidence, status FROM ocr_results WHERE status='DONE' LIMIT 1")
    assert row["engine"] == "PP-OCRv6-small" and row["languages"] == "zh,en,id" and row["confidence"] > 0.8


def test_low_confidence_flagged(pctx):
    st = q1(pctx, "SELECT ocr_status FROM document_versions WHERE file_name='低质量_废水许可证.jpg'")[0]
    assert st == "LOW_CONFIDENCE"


def test_chunks_have_locators(pctx):
    rows = pctx.conn.execute("SELECT c.locator, v.ext FROM chunks c JOIN document_versions v ON v.version_id=c.version_id").fetchall()
    assert rows
    for loc, ext in rows:
        d = json.loads(loc)
        if ext == ".pdf":
            assert d.get("page")
        if ext == ".xlsx":
            assert d.get("sheet") and d.get("row_start")
        if ext == ".pptx":
            assert d.get("slide")
        assert locator_text(d)
    orphan = q1(pctx, "SELECT COUNT(*) FROM chunks c LEFT JOIN document_versions v ON v.version_id=c.version_id WHERE v.version_id IS NULL")[0]
    assert orphan == 0


def test_fts_search_filters(pctx):
    hits = fts.fts_search(pctx.conn, "范围一", SearchFilters())
    assert hits and any("12,345" in h["text"] for h in hits)
    assert fts.fts_search(pctx.conn, '"范围一温室气体"', SearchFilters(), mode="phrase")
    only_x = fts.fts_search(pctx.conn, "LTIFR", SearchFilters(exts=[".xlsx"]))
    assert all(h["ext"] == ".xlsx" for h in only_x)
    assert fts.fts_search(pctx.conn, "Surabaya", SearchFilters(languages=["id"]))
    t = fts.fts_search(pctx.conn, "Jakarta", SearchFilters(), scope="title")
    assert t


def test_metadata_search_and_tags(pctx):
    did = q1(pctx, "SELECT document_id FROM documents WHERE title LIKE '%数据汇总%'")[0]
    pctx.docs.add_tags([did], ["排放数据集"])
    pctx.docs.update_metadata(did, {"department": "可持续发展部", "year": "2025", "doc_type": "数据表"})
    res = fts.metadata_search(pctx.conn, "可持续发展部")
    assert [r["document_id"] for r in res] == [did]
    assert fts.metadata_search(pctx.conn, "", SearchFilters(tags=["排放数据集"]))
    assert fts.fts_search(pctx.conn, "排放数据集", scope="meta")


def test_vector_cross_lingual(pctx):
    r = pctx.retriever.search("电力中可再生能源的比例", mode="vector", top_k=5, rerank=False)
    assert r.mode_used == "vector" and r.hits
    assert any("37%" in h.text for h in r.hits[:5])
    r2 = pctx.retriever.search("hazardous waste management", mode="vector", top_k=6, rerank=False)
    assert any(h.language in ("id", "zh") for h in r2.hits), "英文查询命中其他语言资料"


def test_hybrid_rerank(pctx):
    r = pctx.retriever.search("Berapa emisi cakupan 1 Pabrik Surabaya?", mode="hybrid", top_k=5)
    assert r.reranked and r.hits
    assert "2.150" in r.hits[0].text


def test_rag_answer_with_citations(pctx):
    ids = [x[0] for x in pctx.conn.execute("SELECT document_id FROM documents")]
    pctx.docs.set_governance(ids, "APPROVED")
    a = pctx.rag.ask("华东制造基地2025年范围一温室气体排放量是多少？")
    assert a.status == "ANSWERED", a.answer
    assert "12,345" in a.answer or "12345" in a.answer
    cited = [c for c in a.citations if c.cited]
    assert cited and all(c.document_id and c.version_id and c.chunk_id and c.locator and c.excerpt for c in cited)
    rows = pctx.conn.execute("SELECT * FROM qa_citations WHERE qa_id=?", (a.qa_id,)).fetchall()
    assert len(rows) == len(a.citations)
    loaded = pctx.rag.load(a.qa_id)
    assert loaded.answer == a.answer and len(loaded.citations) == len(a.citations)


def test_rag_no_evidence(pctx):
    a = pctx.rag.ask("公司在火星基地建设了多少座核聚变反应堆？")
    assert a.status == "INSUFFICIENT" and a.answer == "知识库中未找到足够依据"


def test_rag_governance_scope(pctx):
    ids = [x[0] for x in pctx.conn.execute("SELECT document_id FROM documents")]
    pctx.docs.set_governance(ids, "DRAFT")
    a = pctx.rag.ask("华东制造基地2025年范围一温室气体排放量是多少？")
    assert a.status == "INSUFFICIENT" and not a.citations, "默认 RAG 不得使用草稿"
    b = pctx.rag.ask("华东制造基地2025年范围一温室气体排放量是多少？", allow_draft=True)
    assert b.citations
    pctx.docs.set_governance(ids, "APPROVED")
    exp = [x[0] for x in pctx.conn.execute("SELECT document_id FROM documents WHERE title LIKE '%华东基地%'")]
    pctx.docs.set_validity(exp, False)
    c = pctx.rag.ask("华东制造基地2025年范围一温室气体排放量是多少？")
    assert all(ci.document_id not in exp for ci in c.citations), "无效文档不进入 RAG"
    pctx.docs.set_validity(exp, True)


def test_knowledge_items(pctx):
    n = q1(pctx, "SELECT COUNT(*) FROM knowledge_items")[0]
    assert n > 10
    topics = {r[0] for r in pctx.conn.execute("SELECT DISTINCT esg_topic FROM knowledge_items")}
    assert "E-气候与排放" in topics


def test_db_integrity_after_pipeline(pctx):
    from esgxk.db.database import check_file
    chk = check_file(pctx.dr.db_path)
    assert chk["integrity_ok"] and chk["foreign_key_errors"] == 0


def test_degradation_without_ai(ctx_noai):
    c = ctx_noai
    c.imports.quick_import(c.conn, "FOLDER", [str(CORPUS / "en")])
    rid = process_all(c, "LOCAL_KNOWLEDGE_STANDARD")
    r = c.runs.get_run(rid)
    assert r["status"] == "COMPLETED" and r["failed"] == 0
    st = {x[0] for x in c.conn.execute("SELECT embed_status FROM document_versions")}
    assert st == {"ASSET_UNAVAILABLE"}
    ocr = q1(c, "SELECT ocr_status FROM document_versions WHERE file_name='Scanned_Supplier_Code.pdf'")[0]
    assert ocr == "ASSET_UNAVAILABLE"
    res = c.retriever.search("renewable electricity", mode="hybrid")
    assert res.mode_used == "fts" and res.hits and any("降级" in n for n in res.notes)
    ids = [x[0] for x in c.conn.execute("SELECT document_id FROM documents")]
    c.docs.set_governance(ids, "APPROVED")
    a = c.rag.ask("What share of electricity was renewable in 2025?")
    assert a.status == "EVIDENCE_ONLY" and a.citations and "不可用" in a.answer
