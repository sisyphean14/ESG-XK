"""分发包真实测试：模拟家人电脑（全新 LOCALAPPDATA，无 Profile），分别验证 ZIP 与 Setup.exe。

1. ZIP：Windows 自带的 Expand-Archive 与资源管理器（Shell.Application）两种方式解压 → 逐文件 SHA-256。
2. Setup.exe 静默安装 → 校验 / 快捷方式（写入测试目录，不碰真实桌面）。
3. Setup.exe 图形界面自动流程（截图）。
4. 覆盖安装（升级）：预置 ESG-XK-Data / profile / AI-Models / portable.flag，安装后原样保留。
5. 拒绝：Program Files、非空非安装目录。
6. 启动安装后的 ESG-XK.exe：出现首次运行对话框（默认“保存在程序文件夹内”），截图后关闭。
"""
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import psutil
from PIL import ImageGrab

ctypes.windll.shcore.SetProcessDpiAwareness(2)      # 与屏幕截图使用同一像素坐标
DIST = Path(sys.argv[1])
BASE = Path(sys.argv[2])
VER = "1.1.0"
SETUP = DIST / f"ESG-XK-{VER}-Setup.exe"
ZIP = DIST / f"ESG-XK-{VER}-portable.zip"
if BASE.exists():
    shutil.rmtree(BASE)
BASE.mkdir(parents=True)
report: dict = {}
env = {k: v for k, v in os.environ.items() if not k.startswith("ESGXK_")}
env["LOCALAPPDATA"] = str(BASE / "fresh-localappdata")          # 模拟没有任何 ESG-XK 配置的新电脑


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(4 << 20), b""):
            h.update(b)
    return h.hexdigest()


def verify(folder: Path) -> dict:
    ok = bad = 0
    for line in (folder / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        if line.strip():
            d, n = line.split(None, 1)
            if sha(folder / n.strip().lstrip("*")) == d:
                ok += 1
            else:
                bad += 1
    return {"match": ok, "mismatch": bad}


def run_setup(*args, timeout=900) -> dict:
    res = BASE / f"result-{time.time_ns()}.json"
    t0 = time.time()
    p = subprocess.run([str(SETUP), *args, "--result", str(res)], env=env, timeout=timeout)
    out = json.loads(res.read_text(encoding="utf-8")) if res.exists() else {"ok": False, "error": "no result"}
    out["exit_code"] = p.returncode
    out["seconds"] = round(time.time() - t0, 1)
    return out


# 1 ZIP
z1 = BASE / "zip-expand"
subprocess.run(["powershell", "-NoProfile", "-Command",
                f"Expand-Archive -LiteralPath '{ZIP}' -DestinationPath '{z1}' -Force"], check=True)
report["zip_expand_archive"] = verify(z1 / f"ESG-XK-{VER}-portable")
report["zip_expand_archive"]["quickstart_name_ok"] = (z1 / f"ESG-XK-{VER}-portable" / "先看这里-使用说明.txt").exists()
z2 = BASE / "zip-explorer"
z2.mkdir()
subprocess.run(["powershell", "-NoProfile", "-Command",
                f"$sh = New-Object -ComObject Shell.Application; $sh.NameSpace('{z2}').CopyHere($sh.NameSpace('{ZIP}').Items(), 0x14)"],
               check=True, timeout=900)
report["zip_explorer_shell"] = verify(z2 / f"ESG-XK-{VER}-portable")
# 2 静默安装
t_silent = BASE / "install-silent" / "ESG-XK"
desk = BASE / "desktop"
desk.mkdir()
r = run_setup("--silent", "--target", str(t_silent), "--shortcut-dir", str(desk))
r["verify"] = verify(t_silent)
r["shortcut_exists"] = (desk / "ESG-XK.lnk").exists()
report["setup_silent"] = r
# 3 图形界面自动流程
screens = BASE / "screens"
t_gui = BASE / "install-gui" / "ESG-XK"
r = run_setup("--auto-gui", "--target", str(t_gui), "--shortcut-dir", str(desk), "--screens", str(screens))
r["verify"] = verify(t_gui)
r["screens"] = sorted(p.name for p in screens.glob("*.png"))
report["setup_gui"] = r
# 4 覆盖安装（升级）：用户数据保留
for rel, content in (("ESG-XK-Data/database/esgxk.db", b"USERDB"), ("profile/default.json", b"{}"),
                     ("AI-Models/embedding/x.gguf", b"GGUF"), ("portable.flag", b"flag")):
    p = t_silent / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
(t_silent / "_internal" / "stale-old-file.dll").write_bytes(b"old")
r = run_setup("--silent", "--target", str(t_silent), "--no-shortcut")
r["verify"] = verify(t_silent)
r["user_data_kept"] = all((t_silent / rel).read_bytes() == c for rel, c in (
    ("ESG-XK-Data/database/esgxk.db", b"USERDB"), ("profile/default.json", b"{}"),
    ("AI-Models/embedding/x.gguf", b"GGUF"), ("portable.flag", b"flag")))
r["stale_program_file_removed"] = not (t_silent / "_internal" / "stale-old-file.dll").exists()
report["setup_upgrade"] = r
# 5 拒绝
report["refuse_program_files"] = run_setup("--silent", "--target", r"C:\Program Files\ESG-XK", "--no-shortcut")
other = BASE / "not-empty"
other.mkdir()
(other / "something.txt").write_text("x")
report["refuse_non_empty"] = run_setup("--silent", "--target", str(other), "--no-shortcut")
report["refuse_non_empty"]["untouched"] = sorted(p.name for p in other.iterdir()) == ["something.txt"]

# 6 启动安装后的程序：首次运行对话框
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


t0 = time.time()
proc = subprocess.Popen([str(t_gui / "ESG-XK.exe")], cwd=str(t_gui), env=env)
win = None
while time.time() - t0 < 60:
    w = [x for x in windows_of(proc.pid) if "首次运行" in x[1]]
    if w:
        win = w[0]
        break
    time.sleep(0.5)
first = {"window": win[1] if win else None, "seconds": round(time.time() - t0, 1)}
if win:
    user32.SetForegroundWindow(win[0])
    time.sleep(1.5)
    r = wintypes.RECT()
    user32.GetWindowRect(win[0], ctypes.byref(r))
    ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True).save(screens / "first_run_after_install.png")
    user32.PostMessageW(win[0], 0x0010, 0, 0)     # 关闭（= 点“退出”），不创建任何数据
try:
    first["exit_code"] = proc.wait(timeout=30)
except subprocess.TimeoutExpired:
    proc.kill()
    first["exit_code"] = None
first["no_data_created"] = not (t_gui / "ESG-XK-Data").exists() and not (t_gui / "portable.flag").exists()
report["first_launch"] = first
leftover = [p.info for p in psutil.process_iter(["pid", "exe"])
            if (p.info.get("exe") or "").lower().startswith(str(BASE).lower())]
report["processes_left"] = leftover
(BASE / "distribution_test.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=1))
