"""AppContext：应用服务装配、启动检查、数据目录切换、退出校验。"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from esgxk import APP_VERSION
from esgxk.ai import assets as ai_assets
from esgxk.ai import helpers as helper_reg
from esgxk.ai.resources import ResourceManager, get_profile
from esgxk.ai.runtime import AiRuntime
from esgxk.backup.service import BackupService
from esgxk.core import procutil
from esgxk.core.dataroot import DataRoot, create_data_root, validate_data_root
from esgxk.core.errors import ErrorCode, EsgError
from esgxk.core.lease import FileLease, app_lease, worker_lease
from esgxk.core.logs import LogStore, audit, elog, setup_logging, shutdown_logging
from esgxk.core.profile import Profile, ProfileManager
from esgxk.core.timeutil import now_iso
from esgxk.db.database import Database, check_file, connect
from esgxk.db.migrations import SCHEMA_VERSION, migrate
from esgxk.index.vector_store import VectorStore
from esgxk.processing.run_control import RunController
from esgxk.retrieval.hybrid import Retriever
from esgxk.retrieval.rag import RagService
from esgxk.services.documents import DocumentService
from esgxk.services.importing import ImportManager


@dataclass
class StartupReport:
    data_root: str = ""
    schema_version: int = 0
    migration: dict = field(default_factory=dict)
    integrity: dict = field(default_factory=dict)
    recovered: dict = field(default_factory=dict)
    stale_lease: dict | None = None
    capabilities: dict = field(default_factory=dict)


def init_new_data_root(path: str | Path) -> DataRoot:
    """显式创建并初始化新数据目录（仅在用户确认“创建”时调用）。"""
    dr = create_data_root(path)
    migrate(dr.db_path, snapshot_dir=dr.sub("database"))
    return validate_data_root(dr.root)


class AppContext:
    def __init__(self, profile_manager: ProfileManager | None = None):
        self.pm = profile_manager or ProfileManager()
        self.profile: Profile | None = None
        self.dr: DataRoot | None = None
        self.lease: FileLease | None = None
        self.db: Database | None = None
        self.log_store: LogStore | None = None
        self.resources: ResourceManager | None = None
        self.ai: AiRuntime | None = None
        self.vectors: VectorStore | None = None
        self.runs: RunController | None = None
        self.docs: DocumentService | None = None
        self.imports: ImportManager | None = None
        self.retriever: Retriever | None = None
        self.rag: RagService | None = None
        self.backup: BackupService | None = None
        self.startup_report = StartupReport()
        self._hb_counter = 0

    # ------------------------------------------------------------------ 打开
    @property
    def conn(self) -> sqlite3.Connection:
        assert self.db is not None
        return self.db.main

    def load_profile(self) -> Profile:
        self.profile = self.pm.load()
        return self.profile

    def create_profile(self, data_root: str, *, discover: bool = True) -> Profile:
        prof = Profile(data_root=str(Path(data_root).resolve()))
        if discover:
            rep = ai_assets.discover()
            prof.ai_assets = ai_assets.auto_select(rep)
        self.pm.save(prof)
        self.profile = prof
        return prof

    def open(self) -> StartupReport:
        """按 Profile 打开数据目录。严格模式：数据目录不可用时抛出错误，不做任何回退。"""
        if self.profile is None:
            self.load_profile()
        dr = validate_data_root(self.profile.data_root)
        lease = app_lease(dr.sub("runs"))
        lease.acquire({"app_version": APP_VERSION, "data_root": str(dr.root)})
        try:
            self.log_store = LogStore(dr.log_db_path)
            setup_logging(dr.sub("logs"), role="GUI", store=self.log_store)
            rep = StartupReport(data_root=str(dr.root))
            if lease.recovered_stale:
                rep.stale_lease = lease.recovered_stale
                elog("app", logging.WARNING, f"检测到上次异常退出遗留的数据目录租约（PID {lease.recovered_stale.get('pid')}，"
                                             f"心跳 {lease.recovered_stale.get('heartbeat_at')}），已判定为 stale 并回收")
            rep.migration = migrate(dr.db_path, snapshot_dir=dr.sub("database"))
            rep.schema_version = SCHEMA_VERSION
            self.dr, self.lease = dr, lease
            self.db = Database(dr.db_path)
            rep.integrity = check_file(dr.db_path)
            if not rep.integrity["integrity_ok"]:
                elog("app", logging.CRITICAL, f"启动完整性检查失败：{rep.integrity}", error_code=ErrorCode.DB_INTEGRITY_FAILED)
            self._build_services()
            rep.recovered = self.runs.recover_on_startup()
            self.imports.recover_on_startup(self.conn)
            rep.capabilities = ai_assets.capability_state(self.profile.ai_assets)
            self.startup_report = rep
            elog("app", logging.INFO, f"ESG-XK {APP_VERSION} 已启动：Data Root={dr.root} Schema={SCHEMA_VERSION} "
                                      f"integrity={'ok' if rep.integrity['integrity_ok'] else 'FAILED'}")
            audit("启动程序", str(dr.root))
            return rep
        except BaseException:
            self._teardown_partial(lease)
            raise

    def _teardown_partial(self, lease: FileLease) -> None:
        try:
            if self.db:
                self.db.close()
        except Exception:
            pass
        self.db = None
        lease.release()
        self.dr = None
        self.lease = None

    def _build_services(self) -> None:
        prof = get_profile(self.profile.resource_profile)
        self.resources = ResourceManager(self.dr.sub("runs"), prof, "GUI")
        self.ai = AiRuntime(self.dr, self.profile.ai_assets, prof, conn_factory=lambda: connect(self.dr.db_path),
                            resources=self.resources)
        self.vectors = VectorStore(self.dr.sub("vector"))
        self.imports = ImportManager(self.dr)
        self.runs = RunController(self.dr, self.conn, profile_dir=self.pm.directory,
                                  on_before_start=self._before_worker_start, on_estop_local=self._estop_local)
        self.docs = DocumentService(self.conn)
        self.retriever = Retriever(self.conn, self.vectors, self.ai)
        self.rag = RagService(self.conn, self.retriever)
        self.backup = BackupService(self.dr, profile_path=self.pm.path, run_active=self.run_active,
                                    close_db=self._close_db_for_restore, reopen_db=self._reopen_db_after_restore)

    def query_services(self) -> tuple[Retriever, RagService, sqlite3.Connection]:
        """为后台线程创建独立连接的检索 / 问答服务（SQLite 连接不跨线程共享）。调用方负责关闭连接。"""
        conn = connect(self.dr.db_path)
        retriever = Retriever(conn, self.vectors, self.ai)
        return retriever, RagService(conn, retriever), conn

    def _before_worker_start(self) -> None:
        # 16GB Profile：处理任务启动前释放交互式模型，避免多个大模型同时驻留
        released = self.ai.release_all()
        if released:
            elog("app", logging.INFO, f"开始处理前释放交互式模型：{released}")

    def _estop_local(self) -> list[dict]:
        out = self.ai.release_all() if self.ai else []
        if self.imports and self.imports.busy:
            self.imports.cancel(self.conn)
            out.append({"kind": "IMPORT", "pid": os.getpid(), "port": None})
        return out

    def run_active(self) -> bool:
        return bool(self.runs and (self.runs.active_run() or self.runs.worker_alive()))

    def _close_db_for_restore(self) -> None:
        if self.runs:
            self.runs.close()          # 停止并等待 Watchdog 线程释放其数据库连接
        if self.db:
            self.db.checkpoint()
            self.db.close()
        if self.vectors:
            self.vectors.invalidate()

    def _reopen_db_after_restore(self) -> None:
        self.db = Database(self.dr.db_path)
        self._build_services()

    # ------------------------------------------------------------------ 维护
    def heartbeat(self) -> None:
        """由 GUI 定时刷新调用：只更新租约心跳，不触发任何处理。"""
        if self.lease:
            self.lease.heartbeat()

    def update_profile(self, **changes) -> None:
        for k, v in changes.items():
            setattr(self.profile, k, v)
        self.pm.save(self.profile)
        if self.ai:
            self.ai.update_assets(self.profile.ai_assets, get_profile(self.profile.resource_profile))
        if self.resources:
            self.resources.profile = get_profile(self.profile.resource_profile)
        audit("修改设置", ", ".join(changes.keys()))

    def switch_data_root(self, new_root: str, *, create: bool = False) -> StartupReport:
        """切换数据目录（需无运行中任务）。create=True 时显式创建新目录。"""
        if self.run_active():
            raise EsgError(ErrorCode.RUN_ALREADY_ACTIVE, "请先安全停止处理任务再切换数据目录")
        if self.imports and self.imports.busy:
            raise EsgError(ErrorCode.RUN_ALREADY_ACTIVE, "导入进行中，请先暂停或取消导入")
        target = Path(new_root).resolve()
        if create:
            init_new_data_root(target)
        else:
            validate_data_root(target)
        # 先验证目标可获取租约，再关闭当前
        probe = app_lease(target / "runs")
        if probe.probe().held:
            raise EsgError(ErrorCode.PROFILE_DATA_ROOT_LOCKED, str(target))
        old = self.profile.data_root
        self.close()
        self.profile.data_root = str(target)
        try:
            rep = self.open()
        except BaseException:
            self.profile.data_root = old
            self.open()
            raise
        self.pm.save(self.profile)
        audit("切换数据目录", f"{old} → {target}")
        return rep

    # ------------------------------------------------------------------ 关闭
    def shutdown_check(self) -> dict:
        """退出前核对：Worker / Helper / 租约 / 端口。"""
        out = {"workers": 0, "helpers": 0, "ports": [], "worker_lease_held": False}
        if self.runs and self.runs.worker_alive():
            out["workers"] = 1
        if self.db:
            try:
                helper_reg.reconcile(self.conn)
                hs = helper_reg.running(self.conn)
                out["helpers"] = len(hs)
                out["ports"] = [h["port"] for h in hs if h.get("port") and procutil.port_is_listening(h["port"])]
            except sqlite3.Error:
                pass
        if self.dr:
            out["worker_lease_held"] = worker_lease(self.dr.sub("runs")).probe().held
        return out

    def close(self) -> dict:
        report = {"at": now_iso()}
        if self.imports:
            self.imports.cancel(self.conn if self.db else None)
            self.imports.wait(10)
        if self.ai:
            self.ai.release_all()
        if self.runs and self.runs.worker_alive():
            # 正常路径应先安全停止；此处为兜底
            self.runs.emergency_stop()
        if self.runs:
            self.runs.close()
        if self.resources:
            self.resources.release_all()
        report.update(self.shutdown_check())
        if self.dr:
            report["app_lease_released"] = True
        elog("app", logging.INFO, f"退出核对：Worker={report.get('workers')} Helper={report.get('helpers')} "
                                  f"端口={report.get('ports')} 处理租约占用={report.get('worker_lease_held')}")
        audit("退出程序")
        if self.db:
            self.db.checkpoint()
            self.db.close()
            self.db = None
        if self.lease:
            self.lease.release()
            self.lease = None
        if self.dr:
            try:
                from esgxk.core.fsutil import atomic_write_json
                atomic_write_json(self.dr.sub("runs") / "last_shutdown.json", report)
            except OSError:
                pass
        shutdown_logging()
        self.log_store = None
        return report
