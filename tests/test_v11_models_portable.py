"""V1.1：模型下载中心（下载器 / 清单）、扫描电脑、便携模式与数据目录迁移。

下载器使用本机 HTTP 服务器（127.0.0.1）真实测试：断点续传、SHA-256 不一致拒绝、换源、取消、无进展超时。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from esgxk.ai import downloader as dl
from esgxk.ai.scanner import AssetScanner

PAYLOAD = os.urandom(3 * 1024 * 1024 + 123)
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()


class _Handler(BaseHTTPRequestHandler):
    ranges: list = []

    def log_message(self, *a):  # noqa: D401
        pass

    def do_GET(self):  # noqa: N802
        path = self.path
        if path.startswith("/404/"):
            self.send_error(404)
            return
        body = PAYLOAD
        if path.startswith("/bad/"):
            body = bytes(len(PAYLOAD))
        rng = self.headers.get("Range")
        start = 0
        if rng and not path.startswith("/norange/"):
            m = re.match(r"bytes=(\d+)-", rng)
            start = int(m.group(1))
            _Handler.ranges.append(start)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body) - start))
        self.end_headers()
        data = body[start:]
        try:
            if path.startswith("/stall/"):
                self.wfile.write(data[:65536])
                self.wfile.flush()
                time.sleep(30)
                return
            if path.startswith("/slow/"):
                for i in range(0, len(data), 16384):
                    self.wfile.write(data[i:i + 16384])
                    self.wfile.flush()
                    time.sleep(0.05)
                return
            self.wfile.write(data)
        except (ConnectionError, OSError):
            pass


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.daemon_threads = True
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _file(sources, sha=PAYLOAD_SHA, size=len(PAYLOAD), name="model.gguf"):
    return {"name": name, "size": size, "sha256": sha, "sources": sources}


def _nolog(_m):
    pass


def test_catalog_integrity():
    cat = dl.load_catalog()
    ids = [i["id"] for i in cat["items"]]
    assert len(ids) == len(set(ids))
    kinds = {"llama_server", "embedding_model", "reranker_model", "llm_model", "ocr_python", "ocr_model_root"}
    for it in cat["items"]:
        assert it["kind"] in kinds and it["title"] and it["subdir"] and it["files"]
        for f in it["files"]:
            assert re.fullmatch(r"[0-9a-f]{64}", f["sha256"]), f
            assert f["size"] > 0 and f["sources"]
            assert all(s.startswith("https://") for s in f["sources"])
    assert {i["kind"] for i in cat["items"]} == kinds
    rt = next(i for i in cat["items"] if i["id"] == "ppocr-runtime")
    assert all("==" in p for p in rt["pip"]["packages"])          # 版本固定
    rec = dl.recommend(cat, {"ram_gb": 16, "nvidia": False, "gpu": ""})
    assert "llama-cpu" in rec and "llama-cuda" not in rec and "ppocr-runtime" in rec
    rec = dl.recommend(cat, {"ram_gb": 32, "nvidia": True, "gpu": "RTX"})
    assert "llama-cuda" in rec


def test_download_verify_ok(server, tmp_path):
    d = dl.Downloader(no_progress_timeout=10)
    seen = []
    out = d.fetch(_file([f"{server}/ok/m.gguf"]), tmp_path / "m.gguf", seen.append, _nolog)
    assert out.read_bytes() == PAYLOAD and seen[-1] == len(PAYLOAD)
    assert not (tmp_path / "m.gguf.part").exists()
    # 已存在且校验通过：不再下载
    logs = []
    d.fetch(_file([f"{server}/404/x"]), tmp_path / "m.gguf", seen.append, logs.append)
    assert any("已存在且校验通过" in m for m in logs)


def test_download_resume_with_range(server, tmp_path):
    half = len(PAYLOAD) // 2
    (tmp_path / "m.gguf.part").write_bytes(PAYLOAD[:half])
    _Handler.ranges.clear()
    dl.Downloader(no_progress_timeout=10).fetch(_file([f"{server}/ok/m.gguf"]), tmp_path / "m.gguf",
                                                lambda n: None, _nolog)
    assert _Handler.ranges == [half]
    assert (tmp_path / "m.gguf").read_bytes() == PAYLOAD


def test_download_resume_server_without_range_restarts(server, tmp_path):
    (tmp_path / "m.gguf.part").write_bytes(b"x" * 1000)
    dl.Downloader(no_progress_timeout=10).fetch(_file([f"{server}/norange/m.gguf"]), tmp_path / "m.gguf",
                                                lambda n: None, _nolog)
    assert (tmp_path / "m.gguf").read_bytes() == PAYLOAD


def test_sha_mismatch_rejected_and_next_source_used(server, tmp_path):
    logs = []
    out = dl.Downloader(no_progress_timeout=10, retries_per_source=1).fetch(
        _file([f"{server}/bad/m.gguf", f"{server}/404/m.gguf", f"{server}/ok/m.gguf"]), tmp_path / "m.gguf",
        lambda n: None, logs.append)
    assert out.read_bytes() == PAYLOAD
    assert any("SHA-256 校验不一致" in m for m in logs)


def test_sha_mismatch_everywhere_fails_without_file(server, tmp_path):
    with pytest.raises(dl.DownloadError):
        dl.Downloader(no_progress_timeout=10, retries_per_source=1).fetch(
            _file([f"{server}/bad/m.gguf"]), tmp_path / "m.gguf", lambda n: None, _nolog)
    assert not (tmp_path / "m.gguf").exists() and not (tmp_path / "m.gguf.part").exists()


def test_cancel_keeps_partial_for_resume(server, tmp_path):
    d = dl.Downloader(no_progress_timeout=10)
    got = {"n": 0}

    def on_bytes(n):
        got["n"] = n
        if n > 200_000:
            d.cancel.set()

    with pytest.raises(dl.DownloadCancelled):
        d.fetch(_file([f"{server}/slow/m.gguf"]), tmp_path / "m.gguf", on_bytes, _nolog)
    part = tmp_path / "m.gguf.part"
    assert part.exists() and 0 < part.stat().st_size < len(PAYLOAD)
    assert not (tmp_path / "m.gguf").exists()


def test_no_progress_timeout(server, tmp_path):
    t0 = time.time()
    with pytest.raises(dl.DownloadError):
        dl.Downloader(no_progress_timeout=2, retries_per_source=1).fetch(
            _file([f"{server}/stall/m.gguf"]), tmp_path / "m.gguf", lambda n: None, _nolog)
    assert time.time() - t0 < 20


def _fake_catalog(server: str) -> dict:
    gguf = b"GGUF" + os.urandom(1024)
    return {"items": [
        {"id": "embed-test", "kind": "embedding_model", "title": "Embedding 测试", "subdir": "embedding",
         "entry": "embed.gguf", "files": [{"name": "embed.gguf", "size": len(gguf),
                                           "sha256": hashlib.sha256(gguf).hexdigest(),
                                           "sources": [f"{server}/x/embed.gguf"]}]},
        {"id": "broken", "kind": "llm_model", "title": "损坏项", "subdir": "llm", "entry": "x.gguf",
         "files": [{"name": "x.gguf", "size": 10, "sha256": "0" * 64, "sources": [f"{server}/404/x.gguf"]}]},
    ]}, gguf


class _Handler2(_Handler):
    blobs: dict = {}

    def do_GET(self):  # noqa: N802
        name = self.path.rsplit("/", 1)[-1]
        if self.path.startswith("/x/") and name in self.blobs:
            body = self.blobs[name]
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def test_manager_installs_writes_manifest_and_isolates_failures(tmp_path, monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler2)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        server = f"http://127.0.0.1:{httpd.server_address[1]}"
        cat, gguf = _fake_catalog(server)
        _Handler2.blobs = {"embed.gguf": gguf}
        m = dl.DownloadManager()
        m.catalog = cat
        monkeypatch.setattr(dl.time, "sleep", lambda s: None)
        target = tmp_path / "AI-Models"
        st = m.start(["broken", "embed-test"], str(target))
        assert m.wait(60)
        status = {i.item_id: i.status for i in st.items}
        assert status == {"broken": "FAILED", "embed-test": "DONE"}      # 单项失败不影响其他项
        path = Path(st.installed["embedding_model"])
        assert path == target / "embedding" / "embed.gguf" and path.read_bytes() == gguf
        man = json.loads((target / dl.MANIFEST).read_text(encoding="utf-8"))
        assert man["items"]["embed-test"]["path"] == str(path)
        # 扫描电脑能从下载清单与文件本身发现该资产
        rep = AssetScanner([str(target)], min_gguf_bytes=0).run_sync()
        assert any(c.kind == "embedding_model" and Path(c.path) == path for c in rep.candidates)
        # 本地文件安装：SHA-256 匹配才安装
        src = tmp_path / "offline" / "embed.gguf"
        src.parent.mkdir()
        src.write_bytes(gguf)
        other = tmp_path / "AI2"
        p2 = m.install_local_file("embed-test", str(src), str(other))
        assert Path(p2).read_bytes() == gguf and src.exists()
        src.write_bytes(b"GGUF" + b"0" * 100)
        with pytest.raises(dl.DownloadError):
            m.install_local_file("embed-test", str(src), str(tmp_path / "AI3"))
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_extract_rejects_zip_slip(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../evil.txt", "x")
    with pytest.raises(dl.DownloadError):
        dl.DownloadManager._extract(z, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


def test_disk_space_check(tmp_path, monkeypatch):
    m = dl.DownloadManager()
    monkeypatch.setattr(dl.shutil, "disk_usage", lambda p: type("U", (), {"free": 1024})())
    with pytest.raises(RuntimeError, match="空间不足"):
        m.start(["embed-qwen3-0.6b"], str(tmp_path / "t"))
    assert not m.busy


# ---------------------------------------------------------------------- 扫描电脑
def test_scanner_finds_assets_read_only_and_cancellable(tmp_path):
    root = tmp_path / "disk"
    (root / "a" / "b").mkdir(parents=True)
    emb = root / "a" / "b" / "Qwen3-Embedding-0.6B-Q8_0.gguf"
    emb.write_bytes(b"GGUF" + b"\0" * 2048)
    rr = root / "a" / "bge-reranker-v2.gguf"
    rr.write_bytes(b"GGUF" + b"\0" * 2048)
    fake = root / "a" / "not-a-model.gguf"
    fake.write_bytes(b"XXXX" + b"\0" * 2048)
    (root / "tools").mkdir()
    (root / "tools" / "llama-server.exe").write_bytes(b"MZ")
    ocr = root / "ocr" / "PP-OCRv5"
    for comp, name in (("det", "PP-OCRv5_mobile_det"), ("rec", "PP-OCRv5_mobile_rec")):
        (ocr / comp).mkdir(parents=True)
        (ocr / comp / "inference.yml").write_text(f"Global:\n  model_name: {name}\n", encoding="utf-8")
        (ocr / comp / "inference.pdiparams").write_bytes(b"\0")
        (ocr / comp / "inference.json").write_text("{}", encoding="utf-8")
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "node_modules" / "x" / "skip-embed.gguf").write_bytes(b"GGUF" + b"\0" * 2048)
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*") if p.is_file()}
    rep = AssetScanner([str(root)], min_gguf_bytes=0).run_sync()
    got = {(c.kind, Path(c.path).name) for c in rep.candidates}
    assert ("embedding_model", emb.name) in got
    assert ("reranker_model", rr.name) in got
    assert ("llama_server", "llama-server.exe") in got
    assert not any(n == "not-a-model.gguf" for _k, n in got)
    assert not any(n == "skip-embed.gguf" for _k, n in got)
    assert any(c.kind == "ocr_model_root" for c in rep.candidates), got
    after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*") if p.is_file()}
    assert before == after                                   # 只读：不修改 / 不新增任何文件
    s = AssetScanner([str(root)], min_gguf_bytes=0)
    s.cancel.set()
    s.run_sync()
    assert s.finished and s.report.truncated


# ---------------------------------------------------------------------- 便携模式 + 迁移
@pytest.fixture()
def portable_env(tmp_path, monkeypatch):
    base = tmp_path / "ESG-XK-1.1.0-portable"
    base.mkdir()
    monkeypatch.setenv("ESGXK_PORTABLE_BASE", str(base))
    monkeypatch.delenv("ESGXK_PROFILE_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    return base


def test_portable_profile_relative_paths_survive_folder_move(portable_env, tmp_path, monkeypatch):
    from esgxk.core import paths
    from esgxk.core.profile import Profile, ProfileManager
    assert not paths.is_portable()
    assert paths.profile_dir() == tmp_path / "localappdata" / "ESG-XK" / "profiles"
    paths.enable_portable()
    assert paths.is_portable() and paths.profile_dir() == portable_env / "profile"
    pm = ProfileManager()
    assert pm.portable
    prof = Profile(data_root=str(portable_env / "ESG-XK-Data"))
    prof.ai_assets.embedding_model = str(portable_env / "AI-Models" / "embedding" / "e.gguf")
    prof.ai_assets.llm_model = r"Z:\elsewhere\llm.gguf"
    pm.save(prof)
    raw = json.loads(pm.path.read_text(encoding="utf-8"))
    assert raw["data_root"] == "ESG-XK-Data" and raw["portable"] is True
    assert raw["ai_assets"]["embedding_model"] == str(Path("AI-Models") / "embedding" / "e.gguf")
    assert raw["ai_assets"]["llm_model"] == r"Z:\elsewhere\llm.gguf"
    # 整个程序文件夹移动到其他位置（例如复制到另一台电脑）
    moved = tmp_path / "USB" / "ESG-XK"
    moved.parent.mkdir()
    portable_env.rename(moved)
    monkeypatch.setenv("ESGXK_PORTABLE_BASE", str(moved))
    p2 = ProfileManager().load()
    assert Path(p2.data_root) == moved / "ESG-XK-Data"
    assert Path(p2.ai_assets.embedding_model) == moved / "AI-Models" / "embedding" / "e.gguf"
    assert p2.ai_assets.llm_model == r"Z:\elsewhere\llm.gguf"


def test_relocate_copy_verify_switch_and_keep_source(portable_env, tmp_path):
    from esgxk.core import paths
    from esgxk.core.profile import ProfileManager
    from esgxk.services.context import AppContext, init_new_data_root
    src = tmp_path / "old-data"
    init_new_data_root(src)
    pm = ProfileManager(tmp_path / "localappdata" / "ESG-XK" / "profiles")
    ctx = AppContext(pm)
    ctx.create_profile(str(src), discover=False)
    ctx.open()
    try:
        from esgxk.core import settings as app_settings
        app_settings.set_(ctx.conn, "rag.top_k", 7)
        managed = src / "managed_files" / "ab" / "doc.bin"
        managed.parent.mkdir(parents=True)
        managed.write_bytes(os.urandom(200_000))
        target = paths.portable_data_root()
        # 目标非空 → 拒绝
        bad = tmp_path / "nonempty"
        bad.mkdir()
        (bad / "x.txt").write_text("x")
        from esgxk.core.errors import EsgError
        with pytest.raises(EsgError):
            ctx.prepare_relocate(str(bad))
        with pytest.raises(EsgError):
            ctx.prepare_relocate(str(src / "sub"))           # 目标位于源目录内
        need = ctx.prepare_relocate(str(target))
        assert need > 0
        old_root = str(ctx.dr.root)
        ctx.close()
        from esgxk.core import relocate
        prog = relocate.RelocateProgress()
        rep = relocate.copy_verified(Path(old_root), target, prog, threading.Event())
        assert rep["sha256_verified"] == rep["files"] and rep["integrity"]["integrity_ok"]
        ctx.finish_relocate(old_root, str(target), portable=True)
        assert Path(ctx.dr.root) == target.resolve()
        assert paths.is_portable() and ctx.pm.portable
        assert int(app_settings.get(ctx.conn, "rag.top_k")) == 7
        assert (target / "managed_files" / "ab" / "doc.bin").read_bytes() == managed.read_bytes()
        assert managed.exists() and (src / "database" / "esgxk.db").exists()      # 源目录保持不变
        raw = json.loads((portable_env / "profile" / "default.json").read_text(encoding="utf-8"))
        assert raw["data_root"] == "ESG-XK-Data"
    finally:
        ctx.close()
    # 重新启动（新的 AppContext）应直接使用程序文件夹内的配置与数据
    ctx2 = AppContext(ProfileManager())
    ctx2.load_profile()
    ctx2.open()
    try:
        assert Path(ctx2.dr.root) == paths.portable_data_root().resolve()
    finally:
        ctx2.close()


def test_relocate_cancel_cleans_target_and_reopens_source(portable_env, tmp_path):
    from esgxk.core import relocate
    from esgxk.core.profile import ProfileManager
    from esgxk.services.context import AppContext, init_new_data_root
    src = tmp_path / "old-data"
    init_new_data_root(src)
    ctx = AppContext(ProfileManager(tmp_path / "prof"))
    ctx.create_profile(str(src), discover=False)
    ctx.open()
    (src / "managed_files" / "big.bin").write_bytes(os.urandom(12 * 1024 * 1024))
    old_root = str(ctx.dr.root)
    ctx.close()
    target = tmp_path / "new-data"
    cancel = threading.Event()
    prog = relocate.RelocateProgress()
    orig = relocate._copy_hash

    def slow_copy(s, d, c, on_bytes):
        cancel.set()
        return orig(s, d, c, on_bytes)

    relocate._copy_hash = slow_copy
    try:
        with pytest.raises(InterruptedError):
            relocate.copy_verified(Path(old_root), target, prog, cancel)
    finally:
        relocate._copy_hash = orig
    assert prog.cancelled and not target.exists()
    ctx.finish_relocate(old_root, None, portable=False)
    try:
        assert str(ctx.dr.root) == old_root and not (portable_env / "portable.flag").exists()
    finally:
        ctx.close()


# ---------------------------------------------------------------------- UI
def test_models_page_and_scan_dialog_ui(ctx_noai, qtbot, tmp_path, monkeypatch):
    ctx = ctx_noai
    from esgxk.ui.main_window import MainWindow
    import esgxk.ui.pages.models_page as mp_mod
    fake_hw = lambda: {"ram_gb": 16.0, "nvidia": False, "gpu": ""}  # noqa: E731
    monkeypatch.setattr(dl, "hardware", fake_hw)
    monkeypatch.setattr(mp_mod, "hardware", fake_hw)        # 页面模块按名称导入
    win = MainWindow(ctx)
    qtbot.addWidget(win)
    page = win.goto("models")
    assert page.table.rowCount() == len(dl.load_catalog()["items"])
    qtbot.waitUntil(lambda: page.hw is not None, timeout=10000)
    checked = set(page._checked_ids())
    assert "llama-cpu" in checked and "ppocr-runtime" in checked and "llama-cuda" not in checked
    # 已配置且可用的同类资产不再推荐
    emb = tmp_path / "have" / "e.gguf"
    emb.parent.mkdir()
    emb.write_bytes(b"GGUF" + bytes(64))
    from dataclasses import replace
    ctx.update_profile(ai_assets=replace(ctx.profile.ai_assets, embedding_model=str(emb)))
    page.select_recommended()
    assert "embed-qwen3-0.6b" not in page._checked_ids() and "llama-cpu" in page._checked_ids()
    page.target.setText(str(tmp_path / "AI-Models"))
    assert "合计下载约" in page.summary.text() and "目标磁盘可用" in page.summary.text()
    from esgxk.ui.dialogs.asset_scan import AssetScanDialog
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "Qwen3-Reranker-0.6B.gguf").write_bytes(b"GGUF" + b"\0" * 100)
    dlg = AssetScanDialog(win.pages["settings"])
    qtbot.addWidget(dlg)
    for i in range(dlg.scope.count()):
        dlg.scope.item(i).setCheckState(Qt.Unchecked)
    from PySide6.QtWidgets import QListWidgetItem
    it = QListWidgetItem("folder")
    it.setData(Qt.UserRole, str(tmp_path / "m"))
    it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
    it.setCheckState(Qt.Checked)
    dlg.scope.addItem(it)
    monkeypatch.setattr("esgxk.ai.scanner.MIN_GGUF_BYTES", 0)
    dlg.start()
    qtbot.waitUntil(lambda: dlg.scanner.finished and not dlg.timer.isActive(), timeout=10000)
    assert dlg.table.rowCount() == 1 and "扫描完成" in dlg.status.text()
    dlg.table.selectRow(0)
    dlg.use_selected()
    assert dlg.chosen == {"reranker_model": str(tmp_path / "m" / "Qwen3-Reranker-0.6B.gguf")}
    win.timer.stop()
    win._closing = True


def test_first_run_dialog_portable_default(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("ESGXK_PORTABLE_BASE", str(tmp_path / "app"))
    from esgxk.core import paths
    from esgxk.ui.app import FirstRunDialog
    d = FirstRunDialog(tmp_path / "custom")
    qtbot.addWidget(d)
    assert d.portable and d.target() == paths.portable_data_root()
    d.rb_custom.setChecked(True)
    assert not d.portable and d.target() == tmp_path / "custom"


class _IndexHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/fast/simple/paddlepaddle/":
            body = b'<a href="../../pkg/paddlepaddle-3.3.1-cp312-cp312-win_amd64.whl#sha256=00">w</a>'
        elif self.path == "/slow/simple/paddlepaddle/":
            body = b'<a href="../../pkg/paddlepaddle-3.3.1-cp312-cp312-win_amd64.whl#sha256=00">w</a>'
        elif self.path.endswith(".whl"):
            body = PAYLOAD[:2 * 1024 * 1024]
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            slow = self.path.startswith("/slow/")
            try:
                for i in range(0, len(body), 65536):
                    self.wfile.write(body[i:i + 65536])
                    if slow:
                        time.sleep(0.2)
            except OSError:
                pass
            return
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_pypi_mirror_ranking_by_measured_speed():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _IndexHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        b = f"http://127.0.0.1:{httpd.server_address[1]}"
        m = dl.DownloadManager()
        m._dl = dl.Downloader()
        st = dl.JobState(target_root=".")
        order = m._rank_indexes(st, [f"{b}/missing/simple", f"{b}/slow/simple", f"{b}/fast/simple"],
                                "paddlepaddle==3.3.1")
        assert order == [f"{b}/fast/simple", f"{b}/slow/simple", f"{b}/missing/simple"]
        assert any("镜像测速" in ln for ln in st.log)
    finally:
        httpd.shutdown()
        httpd.server_close()
