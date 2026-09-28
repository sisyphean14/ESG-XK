"""源文件完整性：全部测试结束后，测试语料的 size / mtime / SHA-256 与基线完全一致（ESG-XK 未修改任何源文件）。"""
import hashlib
import json
from pathlib import Path

from conftest import CESHI


def test_v11_office_and_reliability_sources_unchanged():
    """V1.1.0：DOCX / PPTX / XLSX / 可靠性语料在全部测试后 size / mtime / SHA-256 不变。"""
    _check("V11_BASELINE.json")


def test_source_files_unchanged():
    _check("SOURCE_BASELINE.json")


def _check(name: str):
    baseline = json.loads((CESHI / name).read_text(encoding="utf-8"))
    changed = []
    for path, b in baseline.items():
        p = Path(path)
        if not p.exists():
            changed.append((path, "missing"))
            continue
        st = p.stat()
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        if st.st_size != b["size"] or abs(st.st_mtime - b["mtime"]) > 1e-6 or sha != b["sha256"]:
            changed.append((path, "modified"))
    print(f"SOURCE_INTEGRITY checked={len(baseline)} changed={len(changed)}")
    assert not changed, changed
