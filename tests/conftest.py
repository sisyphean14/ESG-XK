"""测试夹具：每个测试使用独立的临时 Profile 目录与 Data Root，不触碰正式数据。"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

CESHI = Path(r"D:\ESG-XK\ceshi")
CORPUS = CESHI / "corpus"
RUNCTL = CESHI / "runctl"
WORK = CESHI / "work"


@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ESGXK_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("ESGXK_DEFAULT_DATA_ROOT", str(tmp_path / "data"))
    return tmp_path


@pytest.fixture()
def ctx(tmp_env):
    from esgxk.core.profile import ProfileManager
    from esgxk.services.context import AppContext, init_new_data_root

    dr = init_new_data_root(tmp_env / "data")
    c = AppContext(ProfileManager(tmp_env / "profile"))
    c.create_profile(str(dr.root))
    c.open()
    yield c
    if c.db is not None:
        if c.runs and c.runs.worker_alive():
            c.runs.emergency_stop()
        c.close()


@pytest.fixture()
def ctx_noai(tmp_env):
    """AI 资产全部未配置的上下文（验证降级）。"""
    from esgxk.core.profile import ProfileManager
    from esgxk.services.context import AppContext, init_new_data_root

    dr = init_new_data_root(tmp_env / "data")
    c = AppContext(ProfileManager(tmp_env / "profile"))
    c.create_profile(str(dr.root), discover=False)
    c.open()
    yield c
    if c.db is not None:
        c.close()


@pytest.fixture()
def workdir(tmp_path):
    d = WORK / f"t-{os.getpid()}-{tmp_path.name}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def wait_for(pred, timeout=120.0, step=0.3):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def run_status(ctx, run_id):
    return ctx.runs.get_run(run_id)["status"]


def process_all(ctx, pipeline="SEARCHABLE_CORPUS", ids=None, timeout=900):
    ids = ids or [r[0] for r in ctx.conn.execute("SELECT document_id FROM documents WHERE is_deleted=0")]
    rid = ctx.runs.create_run(pipeline, ids)
    ctx.runs.start(rid)
    assert wait_for(lambda: not ctx.runs.worker_alive() and run_status(ctx, rid) not in ("CREATED", "RUNNING"),
                    timeout), "run did not finish"
    ctx.runs.poll()
    return rid
