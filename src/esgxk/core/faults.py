"""故障注入（仅用于可靠性验收测试）。

只有在环境变量 ESGXK_FAULT_INJECT 被显式设置时才生效，格式：
    parse_hang=<文件名标记>;ocr_hang=<标记>;ocr_crash=<标记>;embed_hang=<标记>;worker_hang=<标记>
当被处理文件的文件名包含对应标记时，在真实的子进程 / 真实的调用路径上制造永久阻塞或崩溃，
用于验证 Timeout / Watchdog / 自动跳过机制。正常使用时该变量不存在，本模块不产生任何影响。
"""
from __future__ import annotations

import os
import time

ENV = "ESGXK_FAULT_INJECT"


def _spec() -> dict[str, str]:
    raw = os.environ.get(ENV, "")
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


def active(kind: str, name: str | None, unit_no: int | None = None) -> bool:
    """标记格式 "MARK" 作用于整个文件；"MARK#n" 只作用于该文件的第 n 个处理单元（页 / 图片）。"""
    if not name or ENV not in os.environ:
        return False
    mark = _spec().get(kind)
    if not mark:
        return False
    if "#" in mark:
        m, n = mark.split("#", 1)
        return m in name and unit_no is not None and str(unit_no) == n
    return mark in name


def hang_forever() -> None:  # pragma: no cover - 由超时机制终止
    while True:
        time.sleep(3600)
