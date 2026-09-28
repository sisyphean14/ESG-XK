"""应用路径解析（开发模式与 PyInstaller 冻结模式）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """应用所在目录：冻结模式为 EXE 所在目录，开发模式为工程根目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def resource_dir() -> Path:
    """打包资源目录（esgxk/resources）。"""
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", app_dir()))
        return base / "esgxk" / "resources"
    return Path(__file__).resolve().parents[1] / "resources"


def worker_command() -> list[str]:
    """启动 Processing Worker 的命令前缀。"""
    if is_frozen():
        return [sys.executable, "--worker"]
    return [sys.executable, "-m", "esgxk", "--worker"]


def default_data_root() -> Path:
    """默认 Data Root。

    冻结模式下为 EXE 所在便携目录的上级目录中的 ESG-XK-Data，
    即 D:\\ESG-XK\\jiaofu\\ESG-XK-Data（EXE 位于 jiaofu\\ESG-XK-1.0.0-portable）。
    """
    env = os.environ.get("ESGXK_DEFAULT_DATA_ROOT")
    if env:
        return Path(env)
    if is_frozen():
        return app_dir().parent / "ESG-XK-Data"
    return Path(r"D:\ESG-XK\jiaofu\ESG-XK-Data")


PORTABLE_FLAG = "portable.flag"
PORTABLE_DATA_NAME = "ESG-XK-Data"
PORTABLE_MODELS_NAME = "AI-Models"


def portable_base() -> Path:
    """便携模式的基准目录（程序文件夹）。测试可用 ESGXK_PORTABLE_BASE 覆盖。"""
    env = os.environ.get("ESGXK_PORTABLE_BASE")
    return Path(env) if env else app_dir()


def is_portable() -> bool:
    """程序文件夹内存在 portable.flag → 便携模式：Profile 与数据都保存在程序文件夹内。"""
    return (portable_base() / PORTABLE_FLAG).is_file()


def portable_profile_dir() -> Path:
    return portable_base() / "profile"


def portable_data_root() -> Path:
    return portable_base() / PORTABLE_DATA_NAME


def default_models_dir() -> Path:
    return portable_base() / PORTABLE_MODELS_NAME


def dir_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".esgxk-write-probe-{os.getpid()}"
        probe.write_bytes(b"ok")
        probe.unlink()
        return True
    except OSError:
        return False


def enable_portable() -> None:
    (portable_base() / PORTABLE_FLAG).write_text(
        "ESG-XK 便携模式标记：本机 Profile 保存在 profile 子文件夹，数据默认保存在 ESG-XK-Data 子文件夹。\n"
        "删除本文件将恢复标准模式（Profile 保存在 %LOCALAPPDATA%\\ESG-XK）。\n", encoding="utf-8")


def profile_dir() -> Path:
    """本机 Profile 目录。

    ESGXK_PROFILE_DIR（测试）> 便携模式的 <程序文件夹>\\profile > %LOCALAPPDATA%\\ESG-XK\\profiles。
    """
    env = os.environ.get("ESGXK_PROFILE_DIR")
    if env:
        return Path(env)
    if is_portable():
        return portable_profile_dir()
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "ESG-XK" / "profiles"
