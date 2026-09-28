"""子进程管理：Windows Job Object、进程树终止、端口校验。

每个 ESG-XK 进程（GUI / Worker）拥有一个 KILL_ON_JOB_CLOSE 的 Job Object，
由它启动的 Worker/Helper 均被加入该 Job；拥有者进程退出或被终止时，操作系统强制结束全部子进程，
因此“程序退出后后台继续处理”在机制上不可能发生。
"""
from __future__ import annotations

import ctypes
import logging
import os
import socket
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass

import psutil

log = logging.getLogger("esgxk.tech.proc")

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class ProcessJob:
    """当前进程的 KILL_ON_JOB_CLOSE Job（进程级单例，句柄随进程退出自动关闭）。"""

    _instance: "ProcessJob | None" = None

    def __init__(self) -> None:
        self.handle = _kernel32.CreateJobObjectW(None, None)
        self.ok = bool(self.handle)
        if not self.ok:
            log.error("CreateJobObject 失败：%s", ctypes.get_last_error())
            return
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(self.handle, JobObjectExtendedLimitInformation,
                                                 ctypes.byref(info), ctypes.sizeof(info)):
            log.error("SetInformationJobObject 失败：%s", ctypes.get_last_error())
            self.ok = False

    @classmethod
    def get(cls) -> "ProcessJob":
        if cls._instance is None:
            cls._instance = ProcessJob()
        return cls._instance

    def assign(self, pid: int) -> bool:
        if not self.ok:
            return False
        h = _kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not h:
            log.warning("OpenProcess(%s) 失败：%s", pid, ctypes.get_last_error())
            return False
        try:
            ok = bool(_kernel32.AssignProcessToJobObject(self.handle, h))
            if not ok:
                log.warning("AssignProcessToJobObject(%s) 失败：%s", pid, ctypes.get_last_error())
            return ok
        finally:
            _kernel32.CloseHandle(h)


def spawn(cmd: list[str], *, cwd: str | None = None, env: dict | None = None, stdin=None, stdout=None,
          stderr=None, text: bool = False) -> subprocess.Popen:
    """启动无控制台窗口的子进程并加入当前进程 Job。"""
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr,
        creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        text=text, encoding="utf-8" if text else None, errors="replace" if text else None,
    )
    ProcessJob.get().assign(proc.pid)
    return proc


def process_create_time(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def is_same_process_alive(pid: int | None, create_time: float | None) -> bool:
    if not pid:
        return False
    try:
        p = psutil.Process(int(pid))
        if create_time is not None and abs(p.create_time() - float(create_time)) > 1.0:
            return False
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return False


@dataclass
class KillResult:
    killed: list[int]
    failed: list[int]


def kill_tree(pid: int, create_time: float | None = None, timeout: float = 5.0) -> KillResult:
    """立即终止进程及其全部子孙进程（校验 create_time 防止误杀复用 PID）。"""
    killed: list[int] = []
    failed: list[int] = []
    try:
        root = psutil.Process(int(pid))
        if create_time is not None and abs(root.create_time() - float(create_time)) > 1.0:
            return KillResult([], [])
    except (psutil.NoSuchProcess, ValueError):
        return KillResult([], [])
    except psutil.AccessDenied:
        return KillResult([], [int(pid)])
    try:
        procs = root.children(recursive=True)
    except psutil.Error:
        procs = []
    procs = procs + [root]
    for p in procs:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error:
            failed.append(p.pid)
    gone, alive = psutil.wait_procs(procs, timeout=timeout)
    killed = [p.pid for p in gone]
    failed += [p.pid for p in alive]
    return KillResult(killed, sorted(set(failed)))


def port_is_listening(port: int, host: str = "127.0.0.1") -> bool:
    if not port:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex((host, int(port))) == 0
    finally:
        s.close()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_port_released(port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_is_listening(port):
            return True
        time.sleep(0.1)
    return not port_is_listening(port)


def pid_alive(pid: int | None) -> bool:
    try:
        return bool(pid) and psutil.pid_exists(int(pid))
    except (TypeError, ValueError):
        return False


def current_pid() -> int:
    return os.getpid()
