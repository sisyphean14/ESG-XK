"""模型下载中心：按用户选择下载并安装 AI 资产（仅在用户点击时联网，只下载，不上传任何数据）。

- 目录（catalog.json）中每个文件都固定了大小与 SHA-256；按镜像优先级下载（ModelScope / hf-mirror / HuggingFace、
  npmmirror / python.org、清华 PyPI / PyPI），任何来源的文件都必须通过 SHA-256 校验，否则丢弃并尝试下一来源；
- 断点续传（.part + HTTP Range）、无进展超时、有限重试、随时取消；安装位置由用户在下载前选择；
- 安装完成后校验（GGUF 文件头 / llama-server 可运行 / PP-OCR 运行时可导入 / OCR 模型可解析），
  写入安装目录下的 ESGXK-MODELS.json 清单，并可自动配置到 AI 资产中心。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import psutil

from esgxk import APP_VERSION
from esgxk.core import paths, procutil
from esgxk.core.fsutil import atomic_write_json
from esgxk.core.logs import audit, elog
from esgxk.core.timeutil import now_iso

UA = {"User-Agent": f"ESG-XK/{APP_VERSION} (model-downloader)"}
CHUNK = 1024 * 1024
MANIFEST = "ESGXK-MODELS.json"


def load_catalog() -> dict:
    return json.loads((paths.resource_dir() / "models" / "catalog.json").read_text(encoding="utf-8"))


def item_size(item: dict) -> int:
    return sum(f["size"] for f in item["files"]) + int((item.get("pip") or {}).get("estimated_bytes", 0))


def hardware() -> dict:
    ram_gb = psutil.virtual_memory().total / (1024 ** 3)
    nvidia = False
    smi = shutil.which("nvidia-smi") or (r"C:\Windows\System32\nvidia-smi.exe"
                                         if os.path.exists(r"C:\Windows\System32\nvidia-smi.exe") else None)
    if smi:
        try:
            out = subprocess.run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True,
                                 text=True, timeout=15, creationflags=procutil.CREATE_NO_WINDOW)
            nvidia = out.returncode == 0 and bool(out.stdout.strip())
            gpu = out.stdout.strip().splitlines()[0] if nvidia else ""
        except (OSError, subprocess.SubprocessError):
            gpu = ""
    else:
        gpu = ""
    return {"ram_gb": round(ram_gb, 1), "nvidia": nvidia, "gpu": gpu}


def recommend(catalog: dict, hw: dict) -> list[str]:
    """按本机硬件给出推荐组合（用户可自行勾选调整）。"""
    rec = ["llama-cuda" if hw["nvidia"] else "llama-cpu", "embed-qwen3-0.6b", "rerank-qwen3-0.6b",
           "llm-qwen3-4b" if (hw["ram_gb"] >= 15 or hw["nvidia"]) else "llm-qwen3-1.7b",
           "ocr-models-ppocrv5", "ppocr-runtime"]
    ids = {i["id"] for i in catalog["items"]}
    return [r for r in rec if r in ids]


@dataclass
class ItemState:
    item_id: str
    title: str
    status: str = "WAITING"      # WAITING / DOWNLOADING / VERIFYING / EXTRACTING / INSTALLING / DONE / SKIPPED / FAILED / CANCELLED
    done_bytes: int = 0
    total_bytes: int = 0
    message: str = ""
    installed_path: str = ""


@dataclass
class JobState:
    target_root: str
    items: list[ItemState] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished: bool = False
    cancelled: bool = False
    log: list[str] = field(default_factory=list)
    speed_bps: float = 0.0
    current: str = ""
    installed: dict = field(default_factory=dict)     # kind -> path

    @property
    def done_bytes(self) -> int:
        return sum(i.done_bytes for i in self.items)

    @property
    def total_bytes(self) -> int:
        return sum(i.total_bytes for i in self.items)


class DownloadCancelled(Exception):
    pass


class DownloadError(Exception):
    pass


def _sha256(path: Path, cancel: threading.Event | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled()
            b = fh.read(4 * CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class Downloader:
    def __init__(self, *, no_progress_timeout: float = 60.0, retries_per_source: int = 2):
        self.no_progress_timeout = no_progress_timeout
        self.retries = retries_per_source
        self.cancel = threading.Event()

    def fetch(self, file: dict, dest: Path, on_bytes: Callable[[int], None], log: Callable[[str], None]) -> Path:
        """下载单个文件到 dest（最终路径），断点续传 + 多来源 + SHA-256 校验。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size == file["size"] and _sha256(dest, self.cancel) == file["sha256"]:
            on_bytes(file["size"])
            log(f"已存在且校验通过：{dest.name}")
            return dest
        part = dest.with_name(dest.name + ".part")
        last_err = None
        for src in file["sources"]:
            for attempt in range(self.retries):
                if self.cancel.is_set():
                    raise DownloadCancelled()
                try:
                    self._fetch_one(src, part, file["size"], on_bytes)
                except DownloadCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 — 网络错误：重试 / 换源
                    last_err = f"{type(exc).__name__}: {exc}"
                    log(f"下载失败（{src.split('/')[2]}，第 {attempt + 1} 次）：{last_err}")
                    time.sleep(min(5, 1 + attempt * 2))
                    continue
                on_bytes(part.stat().st_size)
                actual = _sha256(part, self.cancel)
                if actual != file["sha256"]:
                    log(f"SHA-256 校验不一致，已丢弃（来源 {src.split('/')[2]}）：期望 {file['sha256'][:12]}…，实际 {actual[:12]}…")
                    part.unlink(missing_ok=True)
                    last_err = "SHA-256 mismatch"
                    break          # 该来源内容不可信：换下一个来源
                os.replace(part, dest)
                log(f"下载完成并校验通过：{dest.name}（{src.split('/')[2]}）")
                return dest
        raise DownloadError(f"{file['name']} 所有来源均失败：{last_err}")

    def _fetch_one(self, url: str, part: Path, total: int, on_bytes: Callable[[int], None]) -> None:
        have = part.stat().st_size if part.exists() else 0
        if have > total:
            part.unlink()
            have = 0
        if have == total:
            return
        headers = dict(UA)
        if have:
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=self.no_progress_timeout) as r:
            status = getattr(r, "status", 200)
            if have and status != 206:        # 来源不支持续传：从头下载
                have = 0
            mode = "ab" if have else "wb"
            with open(part, mode) as fh:
                done = have
                while True:
                    if self.cancel.is_set():
                        raise DownloadCancelled()
                    b = r.read(CHUNK)          # 每次读取受 no_progress_timeout 约束
                    if not b:
                        break
                    fh.write(b)
                    done += len(b)
                    on_bytes(done)
        if part.stat().st_size != total:
            raise DownloadError(f"大小不一致：{part.stat().st_size} / {total}")


class DownloadManager:
    """在 GUI 进程的后台线程执行下载任务；UI 轮询 state 显示进度。"""

    def __init__(self):
        self.catalog = load_catalog()
        self.state: JobState | None = None
        self._thread: threading.Thread | None = None
        self._dl: Downloader | None = None
        self._proc: subprocess.Popen | None = None
        self.on_complete: Callable[[JobState], None] | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def items(self) -> list[dict]:
        return self.catalog["items"]

    def get_item(self, item_id: str) -> dict:
        return next(i for i in self.catalog["items"] if i["id"] == item_id)

    def required_bytes(self, item_ids: list[str]) -> int:
        return sum(item_size(self.get_item(i)) for i in item_ids)

    def start(self, item_ids: list[str], target_root: str) -> JobState:
        if self.busy:
            raise RuntimeError("已有下载任务在进行")
        root = Path(target_root)
        root.mkdir(parents=True, exist_ok=True)
        need = int(self.required_bytes(item_ids) * 1.15)
        free = shutil.disk_usage(root).free
        if free < need:
            raise RuntimeError(f"目标磁盘剩余空间不足：需要约 {need / 1e9:.1f} GB，可用 {free / 1e9:.1f} GB")
        st = JobState(target_root=str(root))
        for iid in item_ids:
            it = self.get_item(iid)
            st.items.append(ItemState(iid, it["title"], total_bytes=item_size(it)))
        self.state = st
        self._dl = Downloader()
        self._thread = threading.Thread(target=self._run, args=(st,), daemon=True, name="esgxk-downloader")
        self._thread.start()
        audit("开始下载 AI 资产", f"{', '.join(item_ids)} → {root}")
        return st

    def cancel(self) -> None:
        if self._dl:
            self._dl.cancel.set()
        p = self._proc
        if p is not None and p.poll() is None:
            procutil.kill_tree(p.pid)
        if self.state:
            self.state.cancelled = True

    def wait(self, timeout: float | None = None) -> bool:
        if self._thread:
            self._thread.join(timeout)
        return not self.busy

    # ------------------------------------------------------------------ 执行
    def _log(self, st: JobState, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        st.log.append(line)
        elog("download", logging.INFO, msg)

    def _run(self, st: JobState) -> None:
        last = {"t": time.time(), "b": 0}
        try:
            for ist in st.items:
                if self._dl.cancel.is_set():
                    ist.status = "CANCELLED"
                    continue
                item = self.get_item(ist.item_id)
                st.current = item["title"]
                try:
                    path = self._install_item(st, ist, item, last)
                    ist.status, ist.installed_path = "DONE", path
                    st.installed[item["kind"]] = path
                    self._write_manifest(st, item, path)
                    self._log(st, f"✔ {item['title']} 安装完成：{path}")
                except DownloadCancelled:
                    ist.status, ist.message = "CANCELLED", "已取消（已下载部分可断点续传）"
                    self._log(st, f"已取消：{item['title']}")
                except Exception as exc:  # noqa: BLE001 — 单项失败不影响其他项
                    ist.status, ist.message = "FAILED", str(exc)
                    self._log(st, f"✘ {item['title']} 失败：{exc}")
        finally:
            st.finished = True
            st.current = ""
            audit("下载 AI 资产结束", json.dumps({i.item_id: i.status for i in st.items}, ensure_ascii=False))
            if self.on_complete:
                try:
                    self.on_complete(st)
                except Exception as exc:  # noqa: BLE001
                    elog("download", logging.ERROR, f"下载完成回调失败：{exc}", exc=exc)

    def _progress_cb(self, st: JobState, ist: ItemState, base: int, last: dict):
        def cb(done_in_file: int) -> None:
            ist.done_bytes = base + done_in_file
            now = time.time()
            if now - last["t"] >= 1.0:
                st.speed_bps = max(0.0, (st.done_bytes - last["b"]) / (now - last["t"]))
                last["t"], last["b"] = now, st.done_bytes
        return cb

    def _install_item(self, st: JobState, ist: ItemState, item: dict, last: dict) -> str:
        root = Path(st.target_root) / item["subdir"]
        dl_dir = Path(st.target_root) / ".downloads"
        root.mkdir(parents=True, exist_ok=True)
        base = 0
        ist.status = "DOWNLOADING"
        fetched: list[tuple[dict, Path]] = []
        for f in item["files"]:
            if item["kind"] == "ocr_model_root":
                dest = root / f["name"]
            elif f.get("extract") or item["id"] == "ppocr-runtime":
                dest = dl_dir / f["name"]
            else:
                dest = root / f["name"]
            self._dl.fetch(f, dest, self._progress_cb(st, ist, base, last), lambda m: self._log(st, m))
            base += f["size"]
            ist.done_bytes = base
            fetched.append((f, dest))
        for f, dest in fetched:
            if f.get("extract"):
                ist.status = "EXTRACTING"
                self._extract(dest, root)
        if item["id"] == "ppocr-runtime":
            ist.status = "INSTALLING"
            self._install_ppocr_runtime(st, ist, item, root, fetched)
        ist.status = "VERIFYING"
        path = self._entry_path(item, root)
        self._validate(item, path)
        shutil.rmtree(dl_dir, ignore_errors=True) if not any(dl_dir.glob("*.part")) else None
        return str(path)

    @staticmethod
    def _entry_path(item: dict, root: Path) -> Path:
        entry = item.get("entry") or "."
        if entry == ".":
            return root
        direct = root / entry
        if direct.exists():
            return direct
        found = list(root.rglob(entry))
        return found[0] if found else direct

    @staticmethod
    def _extract(zip_path: Path, target: Path) -> None:
        tmp = target.parent / (target.name + ".extract-tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(zip_path) as zf:
            for m in zf.infolist():
                name = m.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    raise DownloadError(f"压缩包包含非法路径：{name}")
            zf.extractall(tmp)
        for p in tmp.iterdir():
            dst = target / p.name
            if dst.exists():
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            shutil.move(str(p), str(dst))
        shutil.rmtree(tmp, ignore_errors=True)

    def _run_logged(self, st: JobState, cmd: list[str], *, cwd: Path, env: dict, watch_dir: Path,
                    no_progress_timeout: float = 900.0, on_growth: Callable[[int], None] | None = None) -> int:
        """运行安装子进程；以“输出行 或 目录增长”作为进展，无进展超时即终止。"""
        self._proc = procutil.spawn(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True)
        last = {"t": time.time()}

        def reader():
            for line in self._proc.stdout:
                line = line.rstrip()
                if line:
                    last["t"] = time.time()
                    if any(k in line for k in ("Collecting", "Downloading", "Installing", "Successfully", "ERROR",
                                               "error", "Requirement")):
                        self._log(st, "pip: " + line[:180])

        th = threading.Thread(target=reader, daemon=True)
        th.start()
        def dir_bytes() -> int:
            total = 0
            for dp, _dn, fns in os.walk(watch_dir):
                for fn in fns:
                    try:
                        total += os.path.getsize(os.path.join(dp, fn))
                    except OSError:
                        pass
            return total

        size_prev = size0 = dir_bytes()
        tick = 0
        while self._proc.poll() is None:
            time.sleep(2)
            tick += 1
            if self._dl.cancel.is_set():
                procutil.kill_tree(self._proc.pid)
                raise DownloadCancelled()
            if tick % 3 == 0:                 # 每约 6 秒以目录增长作为进展依据（pip 下载大包时可能无输出）
                size = dir_bytes()
                if size != size_prev:
                    last["t"] = time.time()
                size_prev = size
                if on_growth:
                    on_growth(size - size0)
            if time.time() - last["t"] > no_progress_timeout:
                procutil.kill_tree(self._proc.pid)
                raise DownloadError(f"安装进程连续 {int(no_progress_timeout)} 秒无进展，已终止")
        th.join(5)
        rc = self._proc.returncode
        self._proc = None
        return rc

    def _install_ppocr_runtime(self, st: JobState, ist: ItemState, item: dict, root: Path, fetched) -> None:
        py = root / "python.exe"
        if not py.exists():
            raise DownloadError("嵌入式 Python 解压失败")
        pth = next(root.glob("python3*._pth"), None)
        if pth is None:
            raise DownloadError("未找到 python3*._pth")
        lines = pth.read_text(encoding="utf-8").splitlines()
        lines = [("import site" if ln.strip() == "#import site" else ln) for ln in lines]
        if "Lib\\site-packages" not in lines:
            lines.insert(1, "Lib\\site-packages")
        if "import site" not in lines:
            lines.append("import site")
        pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PYTHON", "PIP_", "_PYI", "_MEI"))}
        # pip 的临时下载文件也放在安装目录内：既不占用 C 盘，也让“目录增长”可作为下载进展依据
        (root / ".pip-tmp").mkdir(exist_ok=True)
        env.update({"PYTHONNOUSERSITE": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                    "PIP_CACHE_DIR": str(root / ".pip-cache"), "PYTHONUTF8": "1",
                    "TMP": str(root / ".pip-tmp"), "TEMP": str(root / ".pip-tmp")})
        pip_whl = next(dest for f, dest in fetched if f["name"].endswith(".whl"))
        self._log(st, "安装 pip …")
        # 从 wheel 直接运行 pip 安装自身：用 runpy 启动（Windows 下 pip 拒绝以 “…\pip” 路径形式修改自身）
        boot = ("import sys, runpy; sys.path.insert(0, sys.argv[1]); del sys.argv[1]; "
                "runpy.run_module('pip', run_name='__main__', alter_sys=True)")
        rc = self._run_logged(st, [str(py), "-c", boot, str(pip_whl), "install", "--no-index", str(pip_whl),
                                   "--no-warn-script-location"], cwd=root, env=env, watch_dir=root,
                              no_progress_timeout=300)
        if rc != 0:
            raise DownloadError(f"pip 安装失败（退出码 {rc}）")
        pip_cfg = item["pip"]
        ok = False
        base = ist.done_bytes
        est = int(pip_cfg.get("estimated_bytes", 0))

        def on_growth(n: int) -> None:
            ist.done_bytes = base + min(max(0, n), max(0, est - 1))

        for index in self._rank_indexes(st, pip_cfg["indexes"], pip_cfg["packages"][0]):
            if self._dl.cancel.is_set():
                raise DownloadCancelled()
            self._log(st, f"安装 {' '.join(pip_cfg['packages'])}（镜像 {index.split('/')[2]}，可能需要 5–20 分钟）…")
            rc = self._run_logged(st, [str(py), "-m", "pip", "install", "--no-warn-script-location", "--progress-bar",
                                       "off", "--retries", "10", "--timeout", "60", "-i", index, *pip_cfg["packages"]],
                                  cwd=root, env=env, watch_dir=root, on_growth=on_growth)
            if rc == 0:
                ok = True
                break
            self._log(st, f"镜像 {index.split('/')[2]} 安装失败（退出码 {rc}），尝试下一个来源")
        if not ok:
            raise DownloadError("PaddlePaddle / PaddleOCR 安装失败（所有镜像均失败）")
        shutil.rmtree(root / ".pip-cache", ignore_errors=True)
        shutil.rmtree(root / ".pip-tmp", ignore_errors=True)
        ist.done_bytes = ist.total_bytes

    def _rank_indexes(self, st: JobState, indexes: list[str], package_spec: str) -> list[str]:
        """按实测速度排列 PyPI 镜像（各镜像下载同一 wheel 的前 ~1 MB，限时数秒）；探测失败的排在最后。"""
        name = package_spec.split("==")[0]
        tag = "cp312"                       # 嵌入式运行时固定为 Python 3.12
        speeds: dict[str, float] = {}
        for index in indexes:
            if self._dl.cancel.is_set():
                raise DownloadCancelled()
            try:
                page_url = f"{index.rstrip('/')}/{name}/"
                with urllib.request.urlopen(urllib.request.Request(page_url, headers=UA), timeout=10) as r:
                    page = r.read().decode("utf-8", "replace")
                ver = package_spec.split("==")[1] if "==" in package_spec else ""
                m = re.search(rf'href="([^"#]*{re.escape(name)}-{re.escape(ver)}-{tag}-{tag}-win_amd64\.whl)[^"]*"', page)
                if not m:
                    speeds[index] = 0.0
                    continue
                url = urllib.parse.urljoin(page_url, m.group(1))
                req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-2097151"})
                t0, got = time.time(), 0
                with urllib.request.urlopen(req, timeout=8) as r:
                    while time.time() - t0 < 6:
                        b = r.read(65536)
                        if not b:
                            break
                        got += len(b)
                speeds[index] = got / max(0.05, time.time() - t0)
            except Exception as exc:  # noqa: BLE001 — 探测失败不影响安装，仅排序靠后
                self._log(st, f"镜像测速失败：{index.split('/')[2]}（{type(exc).__name__}）")
                speeds[index] = -1.0
        order = sorted(indexes, key=lambda i: -speeds.get(i, -1.0))
        self._log(st, "镜像测速：" + "，".join(f"{i.split('/')[2]} {max(0.0, speeds[i]) / 1024:.0f} KB/s" for i in order))
        return order

    def _validate(self, item: dict, path: Path) -> None:
        kind = item["kind"]
        from esgxk.ai import assets
        if kind in ("embedding_model", "reranker_model", "llm_model"):
            if not assets.is_gguf(path):
                raise DownloadError(f"不是有效的 GGUF 模型：{path}")
        elif kind == "llama_server":
            if not path.is_file():
                raise DownloadError("未找到 llama-server.exe")
            out = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=60,
                                 creationflags=procutil.CREATE_NO_WINDOW, cwd=str(path.parent))
            if "version" not in (out.stdout + out.stderr).lower():
                raise DownloadError(f"llama-server 无法运行：{(out.stdout + out.stderr)[:200]}")
        elif kind == "ocr_model_root":
            if not assets.valid_ocr_model_root(path):
                raise DownloadError("OCR 模型目录不完整")
        elif kind == "ocr_python":
            out = subprocess.run([str(path), "-c", "import paddle, paddleocr; print('OK', paddle.__version__)"],
                                 capture_output=True, text=True, timeout=300, creationflags=procutil.CREATE_NO_WINDOW)
            if "OK" not in out.stdout:
                raise DownloadError(f"PP-OCR 运行时验证失败：{(out.stdout + out.stderr)[-300:]}")

    def _write_manifest(self, st: JobState, item: dict, path: str) -> None:
        mp = Path(st.target_root) / MANIFEST
        try:
            data = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
        except (OSError, ValueError):
            data = {}
        data.setdefault("format", "ESGXK-MODELS")
        data.setdefault("items", {})
        data["items"][item["id"]] = {"kind": item["kind"], "title": item["title"], "path": path,
                                     "files": [{"name": f["name"], "sha256": f["sha256"]} for f in item["files"]],
                                     "installed_at": now_iso(), "by": f"ESG-XK {APP_VERSION}"}
        atomic_write_json(mp, data)

    # ------------------------------------------------------------------ 本地文件安装（手动下载的离线文件）
    def install_local_file(self, item_id: str, file_path: str, target_root: str) -> str:
        item = self.get_item(item_id)
        src = Path(file_path)
        sha = _sha256(src)
        match = next((f for f in item["files"] if f["sha256"] == sha), None)
        if match is None:
            raise DownloadError("该文件的 SHA-256 与目录中任何文件都不一致，拒绝安装")
        root = Path(target_root) / item["subdir"]
        root.mkdir(parents=True, exist_ok=True)
        if match.get("extract"):
            self._extract(src, root)
        else:
            dest = root / match["name"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        return str(self._entry_path(item, root))
