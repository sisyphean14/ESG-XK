"""组装正式交付目录：D:\\ESG-XK\\jiaofu\\ESG-XK-1.0.0-portable。

内容：ESG-XK.exe + 运行时、config 模板、说明文档、VERSION、LICENSES、MANIFEST.json、SHA256SUMS.txt。
不包含：测试数据库、测试文档、用户文件、模型权重、API Key、密码、开发缓存、.git。
"""
import hashlib
import json
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from esgxk import APP_VERSION  # noqa: E402
from esgxk.db.migrations import SCHEMA_VERSION  # noqa: E402

DIST = ROOT / "build" / "dist" / "ESG-XK"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(rf"D:\ESG-XK\jiaofu\ESG-XK-{APP_VERSION}-portable")
PACKAGES = ["PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6", "numpy", "faiss-cpu", "pypdfium2",
            "python-docx", "python-pptx", "openpyxl", "et_xmlfile", "lxml", "pillow", "psutil", "typing_extensions",
            "XlsxWriter", "pyinstaller"]
FORBIDDEN_EXT = {".gguf", ".pdiparams", ".db", ".safetensors", ".bin", ".pt", ".onnx"}


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(4 << 20), b""):
            h.update(b)
    return h.hexdigest()


def git(*args) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def collect_licenses(dest: Path) -> list[dict]:
    dest.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in PACKAGES:
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        lic = dist.metadata.get("License-Expression") or dist.metadata.get("License") or ""
        if not lic or len(lic) > 80:
            cls = [c.split("::")[-1].strip() for c in dist.metadata.get_all("Classifier") or [] if c.startswith("License")]
            lic = ", ".join(cls) or (lic[:80] + "…" if lic else "见许可证文件")
        pdir = dest / f"{dist.metadata['Name']}-{dist.version}"
        n = 0
        for f in dist.files or []:
            fn = str(f).replace("\\", "/")
            base = fn.rsplit("/", 1)[-1].upper()
            if (".dist-info/" in fn and (base.startswith(("LICENSE", "COPYING", "NOTICE", "AUTHORS")) or "/licenses/" in fn.lower())) \
                    or (base.startswith(("LICENSE", "COPYING")) and fn.count("/") <= 2):
                src = Path(f.locate())
                if src.is_file():
                    pdir.mkdir(exist_ok=True)
                    shutil.copy2(src, pdir / (src.parent.name + "__" + src.name if (pdir / src.name).exists() else src.name))
                    n += 1
        rows.append({"name": dist.metadata["Name"], "version": dist.version, "license": lic, "files": n})
    py_lic = Path(sys.base_prefix) / "LICENSE.txt"
    if py_lic.exists():
        (dest / "Python").mkdir(exist_ok=True)
        shutil.copy2(py_lic, dest / "Python" / "LICENSE.txt")
        rows.append({"name": "CPython", "version": sys.version.split()[0], "license": "PSF-2.0", "files": 1})
    lines = ["# 第三方组件许可证清单（ESG-XK V%s 便携版随附组件）" % APP_VERSION, "",
             "| 组件 | 版本 | 许可证 |", "|---|---|---|"]
    lines += [f"| {r['name']} | {r['version']} | {r['license']} |" for r in rows]
    lines += ["", "说明：PySide6 / Qt 以 LGPL-3.0 动态链接方式使用（便携版中以独立 DLL 形式提供，可替换）。",
              "PDFium（pypdfium2）为 Apache-2.0 / BSD-3-Clause。外部 AI 资产（llama.cpp、GGUF 模型、PaddleOCR 运行时与模型）",
              "不随本程序分发，仅在用户本机按原路径引用，其许可证以各资产自身为准。"]
    (dest / "THIRD_PARTY_NOTICES.md").write_text("\n".join(lines), encoding="utf-8")
    return rows


def main() -> None:
    if not (DIST / "ESG-XK.exe").exists():
        raise SystemExit(f"未找到构建产物：{DIST}")
    if "1.0.0" in OUT.name:
        raise SystemExit("禁止覆盖 V1.0.0 冻结交付目录")
    if OUT.exists():
        shutil.rmtree(OUT)
    # 便携模式的用户数据（profile / 数据目录 / 下载的模型）绝不进入交付物
    shutil.copytree(DIST, OUT, ignore=shutil.ignore_patterns("portable.flag", "profile", "ESG-XK-Data", "AI-Models"))
    docs = ROOT / "release_docs"
    for name in ("README_CN.md", "USER_GUIDE_CN.md", "ADMIN_GUIDE_CN.md", "RELEASE_NOTES.md"):
        if (docs / name).exists():
            shutil.copy2(docs / name, OUT / name)
    (OUT / "config").mkdir()
    shutil.copy2(docs / "profile.template.json", OUT / "config" / "profile.template.json")
    for extra in ("ACCEPTANCE_REPORT.md", "FINAL_TEST_REPORT.md"):
        if (docs / extra).exists():
            shutil.copy2(docs / extra, OUT / extra)
    commit = git("rev-parse", "HEAD")
    (OUT / "VERSION.txt").write_text(
        f"ESG-XK {APP_VERSION}\nSCHEMA_VERSION {SCHEMA_VERSION}\nGIT_COMMIT {commit}\nGIT_TAG ESG-XK-V{APP_VERSION}\n"
        f"BUILD_PYTHON {sys.version.split()[0]}\n", encoding="utf-8")
    lic = collect_licenses(OUT / "LICENSES")
    # 交付安全检查
    bad = [str(p) for p in OUT.rglob("*") if p.is_file() and (p.suffix.lower() in FORBIDDEN_EXT or ".git" in p.parts
                                                              or p.name == "portable.flag")]
    if bad:
        raise SystemExit(f"交付目录包含禁止的文件：{bad[:10]}")
    files = []
    for p in sorted(OUT.rglob("*")):
        if p.is_file() and p.name not in ("MANIFEST.json", "SHA256SUMS.txt"):
            files.append({"path": p.relative_to(OUT).as_posix(), "size": p.stat().st_size, "sha256": sha(p)})
    manifest = {"product": "ESG-XK", "version": APP_VERSION, "schema_version": SCHEMA_VERSION, "git_commit": commit,
                "git_tag": f"ESG-XK-V{APP_VERSION}", "exe": "ESG-XK.exe",
                "exe_sha256": next(f["sha256"] for f in files if f["path"] == "ESG-XK.exe"),
                "file_count": len(files), "total_bytes": sum(f["size"] for f in files),
                "excludes": ["测试数据库", "测试文档", "用户源文件", "私人数据", "模型权重", "API Key", "密码", "开发缓存", ".git"],
                "third_party": lic, "files": files}
    (OUT / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"{f['sha256']}  {f['path']}" for f in files]
    lines.append(f"{sha(OUT / 'MANIFEST.json')}  MANIFEST.json")
    (OUT / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(OUT), "files": len(files), "bytes": manifest["total_bytes"],
                      "exe_sha256": manifest["exe_sha256"], "commit": commit}, ensure_ascii=False))


if __name__ == "__main__":
    main()
