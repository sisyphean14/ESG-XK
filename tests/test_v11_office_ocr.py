"""V1.1.0：Office 内嵌图片 OCR 全链路（真实 Parse Helper / PP-OCR / llama.cpp / Worker）。"""
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from conftest import CESHI, process_all, run_status, wait_for
from esgxk.index import fts
from esgxk.parsers.base import locator_text

OFFICE = CESHI / "office"
MAIN_DOCX = "职业安全环境手册_图片OCR.docx"
MAIN_PPTX = "安全培训材料_图片OCR.pptx"
MAIN_XLSX = "排放台账_图片OCR.xlsx"


@pytest.fixture(scope="module")
def octx(tmp_path_factory):
    from esgxk.core.profile import ProfileManager
    from esgxk.services.context import AppContext, init_new_data_root
    base = tmp_path_factory.mktemp("office")
    os.environ["ESGXK_PROFILE_DIR"] = str(base / "profile")
    dr = init_new_data_root(base / "data")
    c = AppContext(ProfileManager(base / "profile"))
    c.create_profile(str(dr.root))
    c.open()
    caps = c.startup_report.capabilities
    if not all(caps[k]["available"] for k in ("ocr", "embedding", "reranker", "llm")):
        pytest.skip(f"本机 AI 资产不完整：{caps}")
    c.imports.quick_import(c.conn, "FOLDER", [str(OFFICE)])
    c._rid = process_all(c, "LOCAL_KNOWLEDGE_STANDARD", timeout=1800)
    ids = [r[0] for r in c.conn.execute("SELECT document_id FROM documents")]
    c.docs.set_governance(ids, "APPROVED")
    yield c
    c.close()
    os.environ.pop("ESGXK_PROFILE_DIR", None)


def imgs(c, file_name):
    return [dict(r) for r in c.conn.execute(
        "SELECT o.* FROM office_images o JOIN document_versions v ON v.version_id=o.version_id WHERE v.file_name=? "
        "ORDER BY o.image_index", (file_name,))]


def test_run_completed(octx):
    r = octx.runs.get_run(octx._rid)
    assert r["status"] == "COMPLETED" and r["failed"] == 0 and r["succeeded"] == r["total"]


def test_docx_image_extraction_and_filtering(octx):
    rows = imgs(octx, MAIN_DOCX)
    assert len(rows) == 19
    skipped = {r["image_index"]: r["skip_reason"] for r in rows if r["ocr_status"] == "SKIPPED"}
    assert set(skipped.values()) == {"ICON", "TOO_SMALL", "TRANSPARENT", "TINY_PIXEL", "BLANK"}
    kinds = {r["relationship_id"] for r in rows}
    assert len(kinds) >= 15
    # 浮动（anchor）、VML、表格单元格图片
    loc = {r["image_index"]: json.loads(r["container_locator"]) for r in rows}
    assert any(l.get("table") for l in loc.values()), "表格内图片"
    assert any(r["shape_id"] is None and r["ocr_status"] == "DONE" for r in rows), "VML 图片（无 docPr）"
    assert all("page" not in l for l in loc.values()), "DOCX 不伪造页码"
    txt = {r["image_index"]: r["ocr_text"] or "" for r in rows}
    assert "WS-07" in txt[13] and "电网因子" in txt[15] and "能源审计" in txt[17]


def test_docx_locator_and_context(octx):
    r = [x for x in imgs(octx, MAIN_DOCX) if "ESG-XK-OCR-UNIQUE-TEST" in (x["ocr_text"] or "")][0]
    loc = json.loads(r["container_locator"])
    assert loc["heading"] == "2 专项检查" and loc["image"] == 4 and loc.get("paragraph")
    assert r["context_before"] == "专项检查通知见下方附图。" and r["relationship_id"] and r["part_name"].startswith("word/media/")
    label = locator_text(loc)
    assert "章节「2 专项检查」" in label and "段附近" in label and "内嵌图片 4" in label


def test_three_language_office_ocr(octx):
    t = {r["image_index"]: r["ocr_text"] or "" for r in imgs(octx, MAIN_DOCX)}
    assert "职业健康" in t[1] and "8小时" in t[1].replace(" ", "")
    assert "Scope 3" in t[2] and "45,600" in t[2]
    assert "pengurangan emisi" in t[3].lower()


def test_pptx_images(octx):
    rows = imgs(octx, MAIN_PPTX)
    assert len(rows) == 8
    s2 = [r for r in rows if r["slide_number"] == 2]
    rot = [r for r in s2 if "应急演练" in (r["ocr_text"] or "")]
    assert rot, "侧向（旋转 90°）截图被正确识别（PP-OCR 文本行方向 + 必要时旋转重试）"
    orow = octx.conn.execute("SELECT rotation FROM ocr_results WHERE unit_id=? AND is_current=1",
                             (rot[0]["source_unit_id"],)).fetchone()
    assert orow["rotation"] in (0, 90, 180, 270)
    print("ROTATED_IMAGE_EVIDENCE", rot[0]["ocr_text"], "rotation_used", orow["rotation"])
    for r in rows:
        assert r["shape_id"] and r["bbox_w"] and r["bbox_h"] is not None
    grp = [r for r in rows if "流程图截图" in (r["ocr_text"] or "")]
    assert grp and grp[0]["slide_number"] == 3, "组合形状中的图片"
    assert any(r["skip_reason"] == "ICON" for r in s2)
    lbl = locator_text(json.loads([r for r in rows if "ESG-XK-PPTX-OCR-UNIQUE" in (r["ocr_text"] or "")][0]["container_locator"]))
    assert "第3张幻灯片" in lbl and "内嵌图片 1" in lbl


def test_xlsx_images(octx):
    rows = imgs(octx, MAIN_XLSX)
    got = {(r["sheet_name"], r["anchor_cell"]) for r in rows}
    assert {("排放台账", "B4"), ("Water", "D5"), ("Water", "A12"), ("Icon", "C2")} <= got
    u = [r for r in rows if r["anchor_cell"] == "A12"][0]
    assert "ESG-XK-XLSX-OCR-UNIQUE" in u["ocr_text"]
    assert "锚点 A12" in locator_text(u["container_locator"])


@pytest.mark.parametrize("marker,fname", [("ESG-XK-OCR-UNIQUE-TEST", MAIN_DOCX), ("ESG-XK-PPTX-OCR-UNIQUE", MAIN_PPTX),
                                          ("ESG-XK-XLSX-OCR-UNIQUE", MAIN_XLSX)])
def test_fts_image_only_keyword(octx, marker, fname):
    body = octx.conn.execute("SELECT COUNT(*) FROM source_units u JOIN document_versions v ON v.version_id=u.version_id "
                             "WHERE v.file_name=? AND u.unit_type<>'office_image' AND u.text LIKE ?",
                             (fname, f"%{marker}%")).fetchone()[0]
    assert body == 0, "正文中不存在该关键词"
    hits = fts.fts_search(octx.conn, marker)
    assert hits and hits[0]["file_name"] == fname and hits[0]["source_type"] == "OFFICE_IMAGE_OCR"


def test_fts_zhiye_jiankang_body_absent(octx):
    hits = fts.fts_search(octx.conn, "职业健康")
    docx_hits = [h for h in hits if h["file_name"] == MAIN_DOCX]
    assert docx_hits and all(h["source_type"] == "OFFICE_IMAGE_OCR" for h in docx_hits)


def test_vector_hits_office_image(octx):
    r = octx.retriever.search("外购商品带来的范围三排放是多少", mode="vector", top_k=5, rerank=False)
    assert any(h.source_type == "OFFICE_IMAGE_OCR" and "45,600" in h.text for h in r.hits)


@pytest.mark.parametrize("marker,fname,where", [("ESG-XK-OCR-UNIQUE-TEST", "职业安全环境手册_图片OCR", "内嵌图片 4"),
                                                ("ESG-XK-PPTX-OCR-UNIQUE", "安全培训材料_图片OCR", "第3张幻灯片")])
def test_rag_cites_office_image(octx, marker, fname, where):
    a = octx.rag.ask(f"哪个文件包含 {marker}？")
    assert a.status == "ANSWERED", a.answer
    cited = [c for c in a.citations if c.cited]
    assert any(c.document_title == fname and c.source_type == "OFFICE_IMAGE_OCR" and where in c.locator_label
               for c in cited), [(c.document_title, c.locator_label, c.source_type) for c in cited]
    row = octx.conn.execute("SELECT source_type FROM qa_citations WHERE qa_id=? AND cited=1", (a.qa_id,)).fetchall()
    assert any(r[0] == "OFFICE_IMAGE_OCR" for r in row)


def test_duplicate_image_cache(octx):
    rows = [dict(r) for r in octx.conn.execute(
        "SELECT o.*, v.file_name FROM office_images o JOIN document_versions v ON v.version_id=o.version_id "
        "WHERE o.ocr_text LIKE '%DUP-IMG-2025%'")]
    assert len(rows) >= 5          # 主文档 3 处 + 文档A + 文档B（+ PPTX）
    assert len({r["image_hash"] for r in rows}) == 1
    assert sum(1 for r in rows if r["cache_hit"] == 0) == 1, "同一图片只 OCR 一次"
    ocr_calls = octx.conn.execute("SELECT COUNT(*) FROM ocr_results o JOIN office_images i ON i.source_unit_id=o.unit_id "
                                  "WHERE i.image_hash=? AND o.cache_hit=0", (rows[0]["image_hash"],)).fetchone()[0]
    assert ocr_calls == 1
    locs = {r["container_locator"] + r["file_name"] for r in rows}
    assert len(locs) == len(rows), "每个出现位置独立 Locator"
    chunks = octx.conn.execute("SELECT COUNT(DISTINCT c.chunk_id) FROM chunks c JOIN office_images i ON "
                               "i.source_unit_id=c.source_unit_id WHERE i.image_hash=?", (rows[0]["image_hash"],)).fetchone()[0]
    assert chunks == len(rows), "每个出现位置都有独立 Chunk，可分别被引用"
    hit = octx.conn.execute("SELECT hit_count FROM ocr_cache WHERE image_hash=?", (rows[0]["image_hash"],)).fetchone()[0]
    assert hit == len(rows) - 1


def test_chunks_source_type_and_knowledge(octx):
    st = {r[0] for r in octx.conn.execute("SELECT DISTINCT source_type FROM chunks")}
    assert "OFFICE_IMAGE_OCR" in st and "BODY" in st
    n = octx.conn.execute("SELECT COUNT(*) FROM chunks WHERE source_type='OFFICE_IMAGE_OCR' AND chunk_type='IMAGE_OCR' "
                          "AND confidence IS NOT NULL").fetchone()[0]
    assert n >= 20
    assert octx.conn.execute("SELECT COUNT(*) FROM knowledge_items WHERE kind='IMAGE'").fetchone()[0] >= 10
    sm = octx.docs.office_summary(octx.conn.execute("SELECT version_id FROM document_versions WHERE file_name=?",
                                                    (MAIN_DOCX,)).fetchone()[0])
    assert sm["images"] == 19 and sm["skipped"] == 5 and sm["failed"] == 0 and sm["ocr_ok"] + sm["low_confidence"] == 14


def test_low_confidence_penalty():
    from esgxk.retrieval.hybrid import Hit, LOW_CONF_PENALTY
    h = Hit(1, 1, 1, None, "t", "f", 1, 1, "{}", None, None, None, None, "zh", "x", confidence=0.5,
            source_type="OFFICE_IMAGE_OCR")
    assert h.low_confidence and LOW_CONF_PENALTY < 1


def test_reprocess_office_images_only(octx):
    vid, did = octx.conn.execute("SELECT version_id, document_id FROM document_versions WHERE file_name=?",
                                 (MAIN_XLSX,)).fetchone()
    before = octx.conn.execute("SELECT COUNT(*) FROM ocr_results WHERE version_id=?", (vid,)).fetchone()[0]
    parse_events = octx.conn.execute("SELECT COUNT(*) FROM processing_events WHERE version_id=? AND stage='PARSE'",
                                     (vid,)).fetchone()[0]
    rid = octx.runs.create_run("SEARCHABLE_CORPUS", [did], start_stage="OCR",
                               options={"force_ocr": True, "ocr_scope": "office_images", "ocr_no_cache": True})
    octx.runs.start(rid)
    assert wait_for(lambda: run_status(octx, rid) in ("COMPLETED", "COMPLETED_WITH_ERRORS") and not octx.runs.worker_alive(),
                    600)
    after = octx.conn.execute("SELECT COUNT(*) FROM ocr_results WHERE version_id=?", (vid,)).fetchone()[0]
    assert after == before + 3, "仅重新 OCR 3 张需识别的图片（跳过的图标不 OCR）"
    assert octx.conn.execute("SELECT COUNT(*) FROM processing_events WHERE version_id=? AND stage='PARSE'",
                             (vid,)).fetchone()[0] == parse_events, "未重新解析正文"
    assert fts.fts_search(octx.conn, "ESG-XK-XLSX-OCR-UNIQUE")


# ====================================================================== 版本 / 安全停止 / 急停
def test_version_change_reuses_cache_for_unchanged_image(ctx, workdir):
    src = workdir / "节水方案.docx"
    shutil.copyfile(CESHI / "office_versions" / "v1_节水方案.docx", src)
    ctx.imports.quick_import(ctx.conn, "FOLDER", [str(workdir)], register_source=True)
    process_all(ctx, "SEARCHABLE_CORPUS", timeout=900)
    did = ctx.conn.execute("SELECT document_id FROM documents").fetchone()[0]
    shutil.copyfile(CESHI / "office_versions" / "v2_节水方案.docx", src)
    os.utime(src, None)
    sid = ctx.conn.execute("SELECT source_id FROM sources").fetchone()[0]
    ctx.imports.importer().run_batch(ctx.imports.importer().create_resync_batch(ctx.conn, sid))
    vers = ctx.docs.versions(did)
    assert [v["version_no"] for v in vers] == [2, 1]
    process_all(ctx, "SEARCHABLE_CORPUS", ids=[did], timeout=900)
    v2 = {r["image_index"]: r for r in ctx.docs.office_images(vers[0]["version_id"])}
    v1 = {r["image_index"]: r for r in ctx.docs.office_images(vers[1]["version_id"])}
    assert v2[1]["image_hash"] == v1[1]["image_hash"] and v2[1]["cache_hit"] == 1, "未变化图片复用缓存"
    assert v2[2]["image_hash"] != v1[2]["image_hash"] and v2[2]["cache_hit"] == 0, "变化图片重新 OCR"
    assert "200" in v2[2]["ocr_text"] and "100" in v1[2]["ocr_text"], "不得沿用旧图片 OCR；历史版本结果保留"
    hits = fts.fts_search(ctx.conn, "年度节水目标")
    assert all(h["is_current"] for h in hits) and any("200" in h["text"] for h in hits)


def _office_ocr_done(ctx):
    return ctx.conn.execute("SELECT COUNT(*) FROM office_images WHERE ocr_status IN ('DONE','LOW_CONFIDENCE')").fetchone()[0]


def test_office_ocr_safe_stop_resume_no_rework(ctx):
    ctx.imports.quick_import(ctx.conn, "FILES", [str(CESHI / "office_runctl" / "多图片培训_24张.pptx")])
    ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents")]
    rid = ctx.runs.create_run("SEARCHABLE_CORPUS", ids, only_stages=["PARSE", "OCR", "CHUNK", "INDEX"])
    ctx.runs.start(rid)
    assert wait_for(lambda: _office_ocr_done(ctx) >= 5, 600)
    ctx.runs.safe_stop(rid)
    assert wait_for(lambda: not ctx.runs.worker_alive(), 300)
    assert run_status(ctx, rid) == "SAFE_STOPPED"
    done_at_stop = _office_ocr_done(ctx)
    assert 5 <= done_at_stop < 24
    cp = json.loads(ctx.runs.get_run(rid)["checkpoint"])
    assert cp["stage"] == "OCR" and cp.get("unit_no") == done_at_stop
    ids_done = {r[0] for r in ctx.conn.execute("SELECT office_image_id FROM office_images WHERE ocr_status='DONE'")}
    ctx.runs.start(rid)
    assert wait_for(lambda: run_status(ctx, rid) == "COMPLETED" and not ctx.runs.worker_alive(), 900)
    assert _office_ocr_done(ctx) == 24
    assert ctx.conn.execute("SELECT COUNT(*) FROM ocr_results").fetchone()[0] == 24, "第1–N张不重复 OCR"
    first_run = {r[0] for r in ctx.conn.execute("SELECT office_image_id FROM office_images WHERE processed_run_id=?",
                                                (rid,))}
    assert ids_done <= first_run
    print("OFFICE_SAFE_STOP_EVIDENCE", json.dumps({"stopped_at_image": done_at_stop, "total": 24, "checkpoint": cp},
                                                  ensure_ascii=False))


def test_office_ocr_emergency_stop(ctx, tmp_env):
    from esgxk.core.profile import ProfileManager
    from esgxk.db.database import check_file
    from esgxk.services.context import AppContext
    ctx.imports.quick_import(ctx.conn, "FILES", [str(CESHI / "office_runctl" / "多图片培训_24张.pptx")])
    ids = [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents")]
    rid = ctx.runs.create_run("SEARCHABLE_CORPUS", ids, only_stages=["PARSE", "OCR", "CHUNK", "INDEX"])
    ctx.runs.start(rid)
    assert wait_for(lambda: _office_ocr_done(ctx) >= 4, 600)
    rep = ctx.runs.emergency_stop()
    assert rep.verified, rep.to_dict()
    assert run_status(ctx, rid) == "EMERGENCY_STOPPED"
    assert not list((ctx.dr.sub("temp") / "ocr").glob("*")), "急停后清理临时图片"
    bad = ctx.conn.execute("SELECT COUNT(*) FROM office_images o WHERE o.ocr_status='DONE' AND (SELECT COUNT(*) FROM "
                           "ocr_results r WHERE r.unit_id=o.source_unit_id AND r.is_current=1)<>1").fetchone()[0]
    assert bad == 0, "无半写入"
    at_stop = _office_ocr_done(ctx)
    ctx.close()
    c2 = AppContext(ProfileManager(tmp_env / "profile"))
    rep2 = c2.open()
    try:
        assert rep2.integrity["integrity_ok"]
        c2.runs.start(rid)
        assert wait_for(lambda: c2.runs.get_run(rid)["status"] == "COMPLETED" and not c2.runs.worker_alive(), 900)
        assert c2.conn.execute("SELECT COUNT(*) FROM office_images WHERE ocr_status='DONE'").fetchone()[0] == 24
        assert check_file(c2.dr.db_path)["integrity_ok"]
        print("OFFICE_ESTOP_EVIDENCE", json.dumps({"estop_ms": rep.duration_ms, "at_stop": at_stop,
                                                   "temp_cleaned": rep.temp_files_cleaned}, ensure_ascii=False))
    finally:
        c2.close()


def test_rotation_retry_selection_unit():
    """单元测试（桩引擎，仅验证选择逻辑）：首次结果差时依次尝试 90/270/180，并选择得分显著更高的方向。"""
    from types import SimpleNamespace
    from esgxk.processing.stages import StageRunner

    class Eng:
        def __init__(self):
            self.calls = []

        def recognize_png(self, png, **kw):
            self.calls.append(png)
            good = png == "rot90"
            return SimpleNamespace(text="正确识别的旋转文字内容" if good else "丨", confidence=0.97 if good else 0.3,
                                   lines=[])

    eng = Eng()
    ctx = SimpleNamespace(max_retry=1, low_conf=0.8, get_ocr=lambda restart=False: eng, abort_now=lambda: False,
                          check_control=lambda: None, touch_progress=lambda: None, progress=lambda *a: None,
                          restart_ocr_engine=lambda: None, run_id="T")
    runner = StageRunner.__new__(StageRunner)
    runner.ctx = ctx
    out, rot, attempts = runner._ocr_unit(lambda r: f"rot{r}", fault=None, timeout=5, rotation_retry=True)
    assert rot == 90 and "正确识别" in out.text and eng.calls[:2] == ["rot0", "rot90"]
    eng2 = Eng()
    ctx.get_ocr = lambda restart=False: eng2
    out2, rot2, _ = runner._ocr_unit(lambda r: "rot90", fault=None, timeout=5, rotation_retry=True)
    assert rot2 == 0 and len(eng2.calls) == 1, "首次结果良好时不做旋转重试"
