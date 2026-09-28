"""本机 Profile：%LOCALAPPDATA%\\ESG-XK\\profiles\\default.json。

strict_no_fallback = true：Profile 中的数据目录失效时只报告“数据目录不可用”，绝不自动切换到新数据库。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from esgxk import APP_VERSION
from esgxk.core import paths
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.fsutil import atomic_write_json, read_json
from esgxk.core.timeutil import now_iso

PROFILE_VERSION = 1
PROFILE_FILENAME = "default.json"
PATH_FIELDS = ("llama_server", "embedding_model", "reranker_model", "llm_model", "ocr_python", "ocr_model_root",
               "model_root", "runtime_root", "tools_root")


def _to_abs(p: str, base: Path) -> str:
    """便携 Profile 中程序文件夹内的路径以相对路径保存；读取时相对程序文件夹解析为绝对路径。"""
    if not p or os.path.isabs(p):
        return p
    return str((base / p).resolve())


def _to_rel(p: str, base: Path) -> str:
    if not p:
        return p
    try:
        return str(Path(p).resolve().relative_to(base.resolve()))
    except (ValueError, OSError):
        return p


@dataclass
class AiAssetPaths:
    llama_server: str = ""
    embedding_model: str = ""
    reranker_model: str = ""
    llm_model: str = ""
    ocr_python: str = ""
    ocr_model_root: str = ""
    model_root: str = ""
    runtime_root: str = ""
    tools_root: str = ""
    gpu_layers: int = 99


@dataclass
class Profile:
    profile_version: int = PROFILE_VERSION
    app_path: str = ""
    app_version: str = APP_VERSION
    data_root: str = ""
    strict_no_fallback: bool = True
    resource_profile: str = "LOW_MEMORY_16G"
    ai_assets: AiAssetPaths = field(default_factory=AiAssetPaths)
    ai_assets_confirmed: bool = False
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        if not isinstance(data, dict):
            raise EsgError(ErrorCode.PROFILE_INVALID, "内容不是 JSON 对象")
        version = int(data.get("profile_version", 0))
        if version < 1 or version > PROFILE_VERSION:
            raise EsgError(ErrorCode.PROFILE_INVALID, f"不支持的 Profile 版本 {version}")
        assets_raw = data.get("ai_assets") or {}
        known = {f for f in AiAssetPaths.__dataclass_fields__}
        assets = AiAssetPaths(**{k: v for k, v in assets_raw.items() if k in known})
        prof = cls(
            profile_version=version,
            app_path=str(data.get("app_path", "")),
            app_version=str(data.get("app_version", APP_VERSION)),
            data_root=str(data.get("data_root", "")),
            strict_no_fallback=True,  # 强制
            resource_profile=str(data.get("resource_profile", "LOW_MEMORY_16G")),
            ai_assets=assets,
            ai_assets_confirmed=bool(data.get("ai_assets_confirmed", False)),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )
        return prof


class ProfileManager:
    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory) if directory else paths.profile_dir()
        self.path = self.directory / PROFILE_FILENAME

    @property
    def portable(self) -> bool:
        """Profile 位于程序文件夹内（便携模式）：程序文件夹内的路径以相对路径保存，整个文件夹可整体移动 / 复制到其他电脑。"""
        try:
            return self.directory.resolve() == paths.portable_profile_dir().resolve()
        except OSError:
            return False

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> Profile:
        if not self.path.exists():
            raise EsgError(ErrorCode.PROFILE_MISSING, str(self.path))
        try:
            data = read_json(self.path)
        except (OSError, ValueError) as exc:
            raise EsgError(ErrorCode.PROFILE_INVALID, f"{self.path}: {exc}") from exc
        prof = Profile.from_dict(data)
        base = paths.portable_base()
        prof.data_root = _to_abs(prof.data_root, base)
        for f in PATH_FIELDS:
            setattr(prof.ai_assets, f, _to_abs(getattr(prof.ai_assets, f), base))
        return prof

    def save(self, profile: Profile) -> None:
        profile.strict_no_fallback = True
        profile.app_path = str(paths.app_dir())
        profile.app_version = APP_VERSION
        if not profile.created_at:
            profile.created_at = now_iso()
        profile.updated_at = now_iso()
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            # 保留上一版本，便于人工回退
            try:
                shutil.copy2(self.path, self.path.with_suffix(".json.bak"))
            except OSError:
                pass
        data = profile.to_dict()
        if self.portable:
            base = paths.portable_base()
            data["data_root"] = _to_rel(data["data_root"], base)
            for f in PATH_FIELDS:
                data["ai_assets"][f] = _to_rel(data["ai_assets"][f], base)
            data["portable"] = True
        atomic_write_json(self.path, data)
