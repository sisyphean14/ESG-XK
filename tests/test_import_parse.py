"""Importer / Parser：SHA-256 去重、版本、重命名、缺失、托管副本、各格式解析与 Locator。"""
import hashlib
import os
import shutil
from pathlib import Path

import pytest

from conftest import CESHI, CORPUS
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.parsers.registry import load_verified_bytes, parse_bytes


def _import(ctx, paths, **kw):
    bid = ctx.imports.quick_import(ctx.conn, kw.pop("kind", "FOLDER"), [str(p) for p in paths], **kw)
    return bid, {r[0]: r[1] for r in ctx.conn.execute(
        "SELECT status, COUNT(*) FROM import_items WHERE batch_id=? GROUP BY status", (bid,))}


def test_recursive_folder_import_and_dedupe(ctx):
    _bid, counts = _import(ctx, [CORPUS], register_source=True)
    assert counts.get("DUPLICATE") == 2          # duplicates/ 中两个不同名相同内容
    assert counts.get("SKIPPED") == 1            # 不支持的格式
    n_docs = ctx.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert n_docs == counts["DONE"]
    # 同名不同内容 → 两个文档
    same = ctx.conn.execute("SELECT COUNT(DISTINCT sha256) FROM document_versions WHERE file_name='年度总结.txt'").fetchone()[0]
    assert same == 2
    # 再次导入：增量扫描全部未变化
    _bid2, counts2 = _import(ctx, [CORPUS])
    assert counts2.get("DONE", 0) == 0 and counts2.get("SKIPPED", 0) >= n_docs


def test_versions_rename_missing(ctx, workdir):
    src = workdir / "policy.md"
    shutil.copyfile(CESHI / "versions" / "v1" / "碳中和路线图.md", src)
    bid, counts = _import(ctx, [workdir], register_source=True)
    did = ctx.conn.execute("SELECT document_id FROM documents").fetchone()[0]
    sid = ctx.conn.execute("SELECT source_id FROM sources").fetchone()[0]
    # 修改 → 新版本（旧版本保留）
    shutil.copyfile(CESHI / "versions" / "v2" / "碳中和路线图.md", src)
    os.utime(src, None)
    b2 = ctx.imports.importer().create_resync_batch(ctx.conn, sid)
    ctx.imports.importer().run_batch(b2)
    vers = ctx.docs.versions(did)
    assert [v["version_no"] for v in vers] == [2, 1]
    assert vers[0]["is_current"] == 1 and vers[1]["is_current"] == 0
    assert vers[1]["valid_to"] and vers[1]["superseded_by"] == vers[0]["version_id"]
    assert vers[0]["sha256"] != vers[1]["sha256"]
    # 重命名 → 同一文档
    renamed = workdir / "policy_renamed.md"
    os.replace(src, renamed)
    b3 = ctx.imports.importer().create_resync_batch(ctx.conn, sid)
    ctx.imports.importer().run_batch(b3)
    acts = [r[0] for r in ctx.conn.execute("SELECT action FROM import_items WHERE batch_id=?", (b3,))]
    assert "RENAMED" in acts
    assert ctx.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert ctx.docs.get(did)["current"]["abs_path"].endswith("policy_renamed.md")
    # 删除 → 标记缺失（不删除数据）
    renamed.unlink()
    ctx.imports.importer().create_resync_batch(ctx.conn, sid)
    assert ctx.docs.get(did)["file_state"] == "MISSING"
    assert len(ctx.docs.versions(did)) == 2


def test_managed_copy_does_not_touch_source(ctx, workdir):
    f = workdir / "m.txt"
    f.write_text("托管副本测试内容 managed copy", encoding="utf-8")
    before = (f.stat().st_size, f.stat().st_mtime, hashlib.sha256(f.read_bytes()).hexdigest())
    _import(ctx, [f], kind="FILES", storage_mode="MANAGED")
    v = ctx.conn.execute("SELECT managed_path, sha256 FROM document_versions").fetchone()
    assert v["managed_path"] and Path(v["managed_path"]).exists()
    assert Path(v["managed_path"]).is_relative_to(ctx.dr.sub("managed_files"))
    assert hashlib.sha256(Path(v["managed_path"]).read_bytes()).hexdigest() == v["sha256"]
    after = (f.stat().st_size, f.stat().st_mtime, hashlib.sha256(f.read_bytes()).hexdigest())
    assert before == after


def test_manual_new_version(ctx, workdir):
    a = workdir / "a.txt"
    a.write_text("版本一", encoding="utf-8")
    b = workdir / "b.txt"
    b.write_text("版本二内容", encoding="utf-8")
    _import(ctx, [a], kind="FILES")
    did = ctx.conn.execute("SELECT document_id FROM documents").fetchone()[0]
    out = ctx.imports.importer().import_file(ctx.conn, str(b), target_document_id=did)
    assert out.action == "NEW_VERSION"
    assert len(ctx.docs.versions(did)) == 2


def _parse(path):
    data = Path(path).read_bytes()
    return parse_bytes(data, Path(path).suffix)


def test_parse_pdf_text_with_pages():
    r = _parse(CORPUS / "zh" / "华东基地2025可持续发展报告.pdf")
    assert r.page_count == 3
    txt = "".join(u.text for u in r.units)
    assert "12,345" in txt and "范围一" in txt
    assert all(u.page for u in r.units)
    assert any(u.unit_type == "heading" for u in r.units)


def test_parse_scanned_pdf_needs_ocr():
    r = _parse(CORPUS / "zh" / "扫描件_环境管理制度.pdf")
    assert r.page_count == 2 and all(u.needs_ocr for u in r.units)


def test_parse_docx_headings_tables():
    r = _parse(CORPUS / "zh" / "ESG信息披露管理办法.docx")
    heads = [u for u in r.units if u.unit_type == "heading"]
    assert any("环境管理" in h.text for h in heads)
    paras = [u for u in r.units if u.unit_type == "paragraph"]
    assert paras[0].locator.get("heading") and paras[0].paragraph_index == 1
    tables = [u for u in r.units if u.unit_type == "table"]
    assert tables and "12,345" in tables[0].text


def test_parse_xlsx_sheets_rows():
    r = _parse(CORPUS / "zh" / "ESG数据汇总表.xlsx")
    sheets = {u.sheet for u in r.units}
    assert sheets == {"排放数据", "Water", "Limbah"}
    u = [x for x in r.units if x.sheet == "排放数据"][0]
    assert u.row_start == 2 and "12345" in u.text


def test_parse_pptx_slides_tables_notes():
    r = _parse(CORPUS / "zh" / "ESG管理层简报.pptx")
    types = {u.unit_type for u in r.units}
    assert {"slide_text", "table", "slide_notes"} <= types
    assert any(u.slide == 3 and "LTIFR" in u.text for u in r.units)
    assert any(u.unit_type == "slide_notes" and "光伏" in u.text for u in r.units)


def test_parse_text_md_csv_images():
    md = _parse(CORPUS / "zh" / "气候风险说明.md")
    assert md.units[0].unit_type == "heading" and md.units[-1].locator["line_start"]
    csv_ = _parse(CORPUS / "en" / "kpi_summary.csv")
    assert "Scope 1 emissions" in csv_.units[0].text
    tif = _parse(CORPUS / "images" / "碳盘查边界.tiff")
    assert len(tif.units) == 2 and all(u.needs_ocr for u in tif.units)
    for f in ("光伏装机.png", "volunteering.jpg", "mangrove_id.webp", "能源审计.bmp"):
        assert _parse(CORPUS / "images" / f).units[0].needs_ocr


@pytest.mark.parametrize("name,code", [("损坏文件.pdf", ErrorCode.PARSE_CORRUPTED),
                                       ("截断文档.docx", ErrorCode.PARSE_CORRUPTED),
                                       ("fake_sheet.xlsx", ErrorCode.PARSE_CORRUPTED),
                                       ("空文件.txt", ErrorCode.PARSE_EMPTY_FILE)])
def test_parse_broken_files_have_error_codes(name, code):
    with pytest.raises(EsgError) as ei:
        _parse(CORPUS / "broken" / name)
    assert ei.value.code == code


def test_long_document_parses():
    r = _parse(CORPUS / "long" / "超长报告.txt")
    assert len(r.units) > 400
    assert any("红树林3.2公顷" in u.text for u in r.units)


def test_verified_bytes_detects_change(workdir):
    f = workdir / "x.txt"
    f.write_text("abc", encoding="utf-8")
    with pytest.raises(EsgError) as ei:
        load_verified_bytes(str(f), "0" * 64)
    assert ei.value.code == ErrorCode.PARSE_SOURCE_CHANGED
    with pytest.raises(EsgError) as ei:
        load_verified_bytes(str(workdir / "none.txt"), "")
    assert ei.value.code == ErrorCode.PARSE_SOURCE_MISSING
