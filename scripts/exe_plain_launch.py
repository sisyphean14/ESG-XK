"""普通方式启动正式 EXE（等同双击，无任何参数）→ 截取真实屏幕 → 发送窗口关闭消息（等同点击 ×）→ 核对退出。

使用验收数据目录的 Profile（已由 --acceptance 运行创建），因此不会弹出首次运行对话框。
"""
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import psutil
from PIL import ImageGrab

EXE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"D:\ESG-XK\jiaofu\ESG-XK-1.0.0-portable\ESG-XK.exe")
BASE = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(r"D:\ESG-XK\ceshi\work\exe_accept")
user32 = ctypes.WinDLL("user32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def windows_of(pid):
    out = []

    def cb(hwnd, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            out.append((hwnd, buf.value))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


env = dict(os.environ)
env["ESGXK_PROFILE_DIR"] = str(BASE / "profile")
t0 = time.time()
proc = subprocess.Popen([str(EXE)], env=env)
win = None
while time.time() - t0 < 90:
    wins = [w for w in windows_of(proc.pid) if "ESG-XK" in w[1]]
    if wins:
        win = wins[0]
        break
    time.sleep(0.5)
result = {"pid": proc.pid, "window_title": win[1] if win else None, "startup_seconds": round(time.time() - t0, 1)}
if win:
    user32.ShowWindow(win[0], 3)  # 最大化
    user32.SetForegroundWindow(win[0])
    time.sleep(4)
    shot = BASE / "plain_launch_screen.png"
    ImageGrab.grab().save(shot)
    result["screenshot"] = str(shot)
    kids_before = [c.name() for c in psutil.Process(proc.pid).children(recursive=True)]
    result["children_while_idle"] = kids_before
    user32.PostMessageW(win[0], 0x0010, 0, 0)  # WM_CLOSE
try:
    rc = proc.wait(timeout=60)
except subprocess.TimeoutExpired:
    rc = None
    proc.kill()
result["exit_code"] = rc
result["still_running"] = psutil.pid_exists(proc.pid)
ls = BASE / "ESG-XK-Data" / "runs" / "last_shutdown.json"
result["last_shutdown"] = json.loads(ls.read_text(encoding="utf-8")) if ls.exists() else None
(BASE / "plain_launch_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=1))
