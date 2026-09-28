"""AI Asset Center：本机 AI 资产的发现与校验。

原则：只在合理候选路径下做有限深度探测（禁止全盘长期扫描）；只读取文件头，
绝不移动 / 复制 / 删除 / 改写模型文件。所有 AI 资产都是可选组件。
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from esgxk.core.profile import AiAssetPaths

KIND_LABELS = {
    "llama_server": "llama.cpp 推理服务", "embedding_model": "Embedding 模型", "reranker_model": "Reranker 模型",
    "llm_model": "本地 LLM", "ocr_python": "PP-OCR Runtime", "ocr_model_root": "PP-OCR 模型",
}

_SCAN_TIME_LIMIT = 15.0
_MAX_ENTRIES = 60000


@dataclass
class AssetCandidate:
    kind: str
    path: str
    label: str
    size: int = 0
    source: str = ""


@dataclass
class DiscoveryReport:
    candidates: list[AssetCandidate] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    truncated: bool = False

    def by_kind(self, kind: str) -> list[AssetCandidate]:
        return [c for c in self.candidates if c.kind == kind]


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def candidate_model_roots() -> list[Path]:
    home = _home()
    roots = [
        Path(r"D:\Document-Knowledge-Hub\models"),
        Path(r"D:\ChenXin-Local-AI\AI-Assets"),
        Path(r"D:\models"), Path(r"C:\models"), Path(r"D:\AI\models"), Path(r"D:\ai\models"),
        Path(r"E:\models"),
        home / "models",
        home / ".lmstudio" / "models",
        home / ".cache" / "lm-studio" / "models",
        home / ".cache" / "huggingface" / "hub",
        Path(os.environ.get("HF_HOME", str(home / ".cache" / "huggingface"))) / "hub",
    ]
    extra = os.environ.get("ESGXK_EXTRA_MODEL_ROOTS", "")
    roots += [Path(p) for p in extra.split(";") if p.strip()]
    seen, out = set(), []
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def candidate_llama_servers() -> list[Path]:
    home = _home()
    fixed = [
        Path(r"D:\Document-Knowledge-Hub\tools\llama-cpp\llama-server.exe"),
        Path(r"D:\llama.cpp\llama-server.exe"), Path(r"C:\llama.cpp\llama-server.exe"),
        home / "llama.cpp" / "llama-server.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "llama.cpp" / "llama-server.exe",
    ]
    fixed += [Path(p) for p in glob.glob(
        r"D:\Document-Knowledge-Hub\migration-packages\staging\*\*\02_AI_ASSETS\tools\llama-cpp\llama-server.exe")]
    return fixed


def candidate_ocr_pythons() -> list[Path]:
    c = [
        Path(r"D:\Document-Knowledge-Hub\ai-runtime\venvs\paddleocr\Scripts\python.exe"),
    ]
    c += [Path(p) for p in glob.glob(
        r"D:\Document-Knowledge-Hub\migration-packages\staging\*\*\02_AI_ASSETS\runtime\ppocr-worker\python.exe")]
    c += [Path(p) for p in glob.glob(
        r"D:\Document-Knowledge-Hub\migration-packages\license-closure\ppocr-worker-v1\rebuilt-runtime\*\python.exe")]
    return c


def candidate_ocr_model_roots() -> list[Path]:
    out = [Path(p) for p in glob.glob(r"D:\Document-Knowledge-Hub\models\ocr\*")]
    out += [Path(p) for p in glob.glob(r"D:\ChenXin-Local-AI\AI-Assets\OCR\*")]
    out += [Path(p) for p in glob.glob(
        r"D:\Document-Knowledge-Hub\migration-packages\staging\*\*\02_AI_ASSETS\models\ocr\*")]
    return out


def is_gguf(path: str | Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"GGUF"
    except OSError:
        return False


def classify_gguf(name: str) -> str | None:
    n = name.lower()
    if "mmproj" in n or "ocr" in n or "vocab" in n:
        return None
    if "rerank" in n:
        return "reranker_model"
    if "embed" in n or "bge-m3" in n or "e5-" in n:
        return "embedding_model"
    return "llm_model"


def valid_ocr_python(path: Path) -> bool:
    if not path.is_file():
        return False
    base = path.parent
    for sp in (base / "Lib" / "site-packages", base.parent / "Lib" / "site-packages"):
        if (sp / "paddleocr").is_dir() and (sp / "paddle").is_dir():
            return True
    return False


def _yml_model_name(yml: Path) -> str | None:
    try:
        for line in yml.read_text(encoding="utf-8", errors="replace").splitlines()[:40]:
            s = line.strip()
            if s.startswith("model_name:"):
                return s.split(":", 1)[1].strip().strip("'\"")
    except OSError:
        return None
    return None


def resolve_ocr_models(root: str | Path) -> dict[str, tuple[str, str]]:
    """在 OCR 模型根目录（最多两层）中按 inference.yml 的 model_name 识别检测 / 识别 / 文本行方向模型。
    返回 {"det": (目录, 模型名), "rec": (...), "ori": (...)}（ori 可缺省）。"""
    base = Path(root)
    found: dict[str, tuple[str, str]] = {}
    if not base.is_dir():
        return found
    cands = [base] + [p for p in base.iterdir() if p.is_dir()]
    for p in list(cands[1:]):
        try:
            cands += [q for q in p.iterdir() if q.is_dir()]
        except OSError:
            pass
    for d in cands:
        yml = d / "inference.yml"
        if not (yml.is_file() and (d / "inference.pdiparams").is_file()):
            continue
        name = _yml_model_name(yml) or d.name
        low = name.lower()
        kind = "det" if low.endswith("_det") else "rec" if low.endswith("_rec") else \
            "ori" if "textline_ori" in low else None
        if kind and kind not in found:
            found[kind] = (str(d), name)
    return found


def valid_ocr_model_root(path: Path) -> bool:
    r = resolve_ocr_models(path)
    return "det" in r and "rec" in r


def _walk_gguf(root: Path, max_depth: int, report: DiscoveryReport, t0: float, counter: list[int]) -> None:
    stack = [(root, 0)]
    while stack:
        if time.time() - t0 > _SCAN_TIME_LIMIT or counter[0] > _MAX_ENTRIES:
            report.truncated = True
            return
        d, depth = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    counter[0] += 1
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if depth < max_depth and not e.name.startswith("."):
                                stack.append((Path(e.path), depth + 1))
                            elif depth < max_depth and e.name in (".cache",):
                                continue
                        elif e.name.lower().endswith(".gguf") and e.stat().st_size > 50 * 1024 * 1024:
                            kind = classify_gguf(e.name)
                            if kind and is_gguf(e.path):
                                report.candidates.append(AssetCandidate(kind, e.path, e.name, e.stat().st_size,
                                                                        str(root)))
                    except OSError:
                        continue
        except OSError:
            continue


def _ollama_models(root: Path, report: DiscoveryReport) -> None:
    """解析 Ollama manifests，把模型层 blob 作为 LLM 候选（只读引用）。"""
    manifests = root / "manifests"
    blobs = root / "blobs"
    if not manifests.is_dir() or not blobs.is_dir():
        return
    for mf in manifests.rglob("*"):
        if not mf.is_file():
            continue
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for layer in data.get("layers", []):
            if layer.get("mediaType") == "application/vnd.ollama.image.model":
                blob = blobs / layer.get("digest", "").replace(":", "-")
                if blob.is_file() and is_gguf(blob):
                    rel = mf.relative_to(manifests).parts
                    name = f"{rel[-2]}:{rel[-1]}" if len(rel) >= 2 else mf.name
                    kind = classify_gguf(name) or "llm_model"
                    report.candidates.append(AssetCandidate(kind, str(blob), f"Ollama {name}", blob.stat().st_size,
                                                            str(root)))


def discover() -> DiscoveryReport:
    t0 = time.time()
    report = DiscoveryReport()
    counter = [0]
    for exe in candidate_llama_servers():
        report.searched.append(str(exe))
        if exe.is_file():
            report.candidates.append(AssetCandidate("llama_server", str(exe), exe.parent.name + "\\llama-server.exe",
                                                    exe.stat().st_size, str(exe.parent)))
    for root in candidate_model_roots():
        report.searched.append(str(root))
        if root.is_dir():
            depth = 7 if "huggingface" in str(root).lower() else 4
            _walk_gguf(root, depth, report, t0, counter)
    for oroot in [Path(r"D:\ChenXin-Local-AI\AI-Assets\LLM\Ollama"), _home() / ".ollama" / "models",
                  Path(os.environ.get("OLLAMA_MODELS", "")) if os.environ.get("OLLAMA_MODELS") else None]:
        if oroot and oroot.is_dir():
            report.searched.append(str(oroot))
            _ollama_models(oroot, report)
    for py in candidate_ocr_pythons():
        report.searched.append(str(py))
        if valid_ocr_python(py):
            report.candidates.append(AssetCandidate("ocr_python", str(py), "PP-OCR runtime " + py.parent.name,
                                                    0, str(py.parent)))
    for mr in candidate_ocr_model_roots():
        report.searched.append(str(mr))
        if mr.is_dir() and valid_ocr_model_root(mr):
            report.candidates.append(AssetCandidate("ocr_model_root", str(mr), mr.name, 0, str(mr.parent)))
    # 去重（同一路径）
    seen, uniq = set(), []
    for c in report.candidates:
        key = (c.kind, os.path.normcase(os.path.abspath(c.path)))
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    report.candidates = uniq
    report.elapsed = round(time.time() - t0, 2)
    return report


def _prefer(cands: list[AssetCandidate], prefer: tuple[str, ...]) -> AssetCandidate | None:
    for p in prefer:
        for c in cands:
            if p.lower() in c.path.lower():
                return c
    return cands[0] if cands else None


def auto_select(report: DiscoveryReport) -> AiAssetPaths:
    """根据发现结果给出默认选择（用户可在 设置 → AI资产中心 修改）。"""
    sel = AiAssetPaths()
    c = _prefer(report.by_kind("llama_server"), ("Document-Knowledge-Hub\\tools",))
    sel.llama_server = c.path if c else ""
    c = _prefer(report.by_kind("embedding_model"), ("qwen3-embedding",))
    sel.embedding_model = c.path if c else ""
    c = _prefer(report.by_kind("reranker_model"), ("qwen3-reranker",))
    sel.reranker_model = c.path if c else ""
    llms = sorted(report.by_kind("llm_model"), key=lambda x: x.size)
    # 16GB 办公电脑：优先 2–4.5GB 的量化模型
    mid = [x for x in llms if 1.5e9 <= x.size <= 4.8e9]
    c = _prefer(mid, ("qwen3.5-4b", "qwen3-4b", "qwen2.5-3b")) or (mid[0] if mid else (llms[0] if llms else None))
    sel.llm_model = c.path if c else ""
    c = _prefer(report.by_kind("ocr_python"), ("1.0.3-license-closed-final", "ppocr-worker"))
    sel.ocr_python = c.path if c else ""
    c = _prefer(report.by_kind("ocr_model_root"), ("Document-Knowledge-Hub\\models\\ocr",))
    sel.ocr_model_root = c.path if c else ""
    if sel.embedding_model:
        sel.model_root = str(Path(sel.embedding_model).parents[1])
    if sel.ocr_python:
        sel.runtime_root = str(Path(sel.ocr_python).parent)
    if sel.llama_server:
        sel.tools_root = str(Path(sel.llama_server).parent)
    return sel


@dataclass
class AssetStatus:
    kind: str
    path: str
    state: str  # NOT_CONFIGURED / MISSING / INVALID / OK
    message: str


def check_asset(kind: str, path: str) -> AssetStatus:
    if not path:
        return AssetStatus(kind, path, "NOT_CONFIGURED", "尚未配置AI资产")
    p = Path(path)
    if kind == "ocr_model_root":
        if not p.is_dir():
            return AssetStatus(kind, path, "MISSING", "目录不存在")
        if not valid_ocr_model_root(p):
            return AssetStatus(kind, path, "INVALID", "未找到 PP-OCR 检测 / 识别模型（inference.yml + inference.pdiparams）")
        if not str(p).isascii():
            return AssetStatus(kind, path, "INVALID", "PP-OCR 模型路径必须为纯 ASCII 路径")
        return AssetStatus(kind, path, "OK", "可用")
    if not p.is_file():
        return AssetStatus(kind, path, "MISSING", "文件不存在")
    if kind in ("embedding_model", "reranker_model", "llm_model"):
        if not is_gguf(p):
            return AssetStatus(kind, path, "INVALID", "不是 GGUF 模型文件")
    if kind == "ocr_python" and not valid_ocr_python(p):
        return AssetStatus(kind, path, "INVALID", "该 Python 环境中未找到 paddleocr / paddle")
    return AssetStatus(kind, path, "OK", "可用")


def check_all(sel: AiAssetPaths) -> dict[str, AssetStatus]:
    return {k: check_asset(k, getattr(sel, k)) for k in KIND_LABELS}


def llama_version(exe: str) -> str:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20,
                             creationflags=0x08000000)
        text = (out.stdout or "") + (out.stderr or "")
        for line in text.splitlines():
            if "version" in line.lower():
                return line.strip()
        return text.strip()[:120]
    except (OSError, subprocess.SubprocessError) as exc:
        return f"无法运行：{exc}"


def capability_state(sel: AiAssetPaths) -> dict[str, dict]:
    """能力级状态（供首页与降级策略使用）。"""
    st = check_all(sel)
    server_ok = st["llama_server"].state == "OK"

    def cap(model_kind: str) -> dict:
        s = st[model_kind]
        if s.state != "OK":
            return {"available": False, "reason": f"{KIND_LABELS[model_kind]}：{s.message}"}
        if not server_ok:
            return {"available": False, "reason": f"llama.cpp 推理服务：{st['llama_server'].message}"}
        return {"available": True, "reason": Path(s.path).name}

    ocr_ok = st["ocr_python"].state == "OK" and st["ocr_model_root"].state == "OK"
    ocr_reason = "PP-OCRv6 " + Path(sel.ocr_model_root).name if ocr_ok else (
        st["ocr_python"].message if st["ocr_python"].state != "OK" else st["ocr_model_root"].message)
    return {
        "embedding": cap("embedding_model"),
        "reranker": cap("reranker_model"),
        "llm": cap("llm_model"),
        "ocr": {"available": ocr_ok, "reason": ocr_reason},
    }


def model_id(path: str) -> str:
    """模型身份：文件名 + 大小（切换模型后旧向量不再参与检索）。"""
    try:
        return f"{Path(path).name}:{Path(path).stat().st_size}"
    except OSError:
        return Path(path).name
