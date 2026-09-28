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


def profile_dir() -> Path:
    """本机 Profile 目录：%LOCALAPPDATA%\\ESG-XK\\profiles（测试可用 ESGXK_PROFILE_DIR 覆盖）。"""
    env = os.environ.get("ESGXK_PROFILE_DIR")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "ESG-XK" / "profiles"
