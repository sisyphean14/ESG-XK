from __future__ import annotations

import datetime as _dt


def now_iso() -> str:
    """本地时间 ISO 字符串（秒级 + 毫秒），用于数据库与日志。"""
    return _dt.datetime.now().isoformat(timespec="milliseconds")


def today_iso() -> str:
    return _dt.date.today().isoformat()


def stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def parse_iso(value: str | None) -> _dt.datetime | None:
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def ts_to_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
