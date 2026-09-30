"""ESG-XK 安装程序（ESG-XK-<版本>-Setup.exe）。

- 把内置的便携版 ZIP 解压到用户选择的文件夹，并按 SHA256SUMS.txt 逐文件校验；
- 可选：创建桌面快捷方式、安装完成后启动；
- 不写注册表、不装服务、不需要管理员权限；卸载 = 删除程序文件夹；
- 覆盖安装（升级）时只替换程序文件，绝不触碰 ESG-XK-Data、profile、AI-Models、portable.flag 等用户数据。

命令行（测试用）：--silent --target DIR [--shortcut-dir DIR] [--no-shortcut]
               --auto-gui --target DIR --shortcut-dir DIR --screens DIR
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

APP_VERSION = "1.1.0"
PAYLOAD_NAME = f"ESG-XK-{APP_VERSION}-portable.zip"
USER_DATA = {"esg-xk-data", "profile", "ai-models", "portable.flag"}     # 覆盖安装时永不删除
FORBIDDEN_ROOTS = ("c:\\program files", "c:\\program files (x86)", "c:\\windows")


def payload_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "payload" / PAYLOAD_NAME


def default_target() -> Path:
    import ctypes
    if ctypes.windll.kernel32.GetDriveTypeW("D:\\") == 3:          # 固定磁盘
        return Path(r"D:\ESG-XK")
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "ESG-XK"


def normalize_choice(folder: Path) -> Path:
    """浏览选择的文件夹：空文件夹 / 已有安装直接使用，否则在其中新建 ESG-XK 子文件夹。"""
    if not folder.exists() or not any(folder.iterdir()) or (folder / "ESG-XK.exe").exists():
        return folder
    return folder / "ESG-XK"


class Installer:
    def __init__(self, payload: Path, target: Path):
        self.payload = payload
        self.target = target
        self.total = 0
        self.done = 0
        self.phase = "准备"
        self.current = ""
        self.error = ""
        self.finished = False
        self.upgrade = False
        self.verified = 0
        self.zf = zipfile.ZipFile(payload)
        names = [i.filename for i in self.zf.infolist()]
        self.prefix = names[0].split("/")[0] + "/"
        self.members = [i for i in self.zf.infolist() if not i.is_dir() and i.filename.startswith(self.prefix)]
        self.size = sum(i.file_size for i in self.members)

    # ------------------------------------------------------------------ 检查
    def check(self) -> str | None:
        t = self.target
        if not t.is_absolute() or len(t.parts) < 2:
            return "请选择一个完整的文件夹路径（例如 D:\\ESG-XK），不能直接安装到磁盘根目录。"
        low = str(t).lower()
        if any(low.startswith(r) for r in FORBIDDEN_ROOTS):
            return "请不要安装到 C:\\Program Files 或 Windows 目录：程序需要在自己的文件夹里保存数据。\n建议安装到 D:\\ESG-XK。"
        if t.exists() and any(t.iterdir()) and not (t / "ESG-XK.exe").exists():
            return f"该文件夹不是空的，也不是 ESG-XK 的安装目录：\n{t}\n请选择空文件夹，或点“浏览…”重新选择。"
        probe = t
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        free = shutil.disk_usage(probe).free
        if free < self.size * 1.1 + 50 * 1024 * 1024:
            return f"磁盘空间不足：需要约 {self.size / 1e9:.2f} GB，可用 {free / 1e9:.2f} GB。"
        try:
            t.mkdir(parents=True, exist_ok=True)
            p = t / f".esgxk-setup-probe-{os.getpid()}"
            p.write_bytes(b"ok")
            p.unlink()
        except OSError as exc:
            return f"无法写入该文件夹：{exc}"
        if self.running():
            return "ESG-XK 正在运行，请先关闭程序（安全停止处理任务后退出），再继续安装。"
        return None

    def running(self) -> bool:
        exe = str((self.target / "ESG-XK.exe").resolve()).lower()
        try:
            import psutil
            for p in psutil.process_iter(["exe"]):
                if (p.info.get("exe") or "").lower() == exe:
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    # ------------------------------------------------------------------ 安装
    def run(self) -> None:
        try:
            t = self.target
            self.upgrade = (t / "ESG-XK.exe").exists()
            if self.upgrade:          # 只移除旧版程序文件（ZIP 中出现的顶层条目），用户数据保持不动
                self.phase = "移除旧版程序文件"
                tops = {m.filename[len(self.prefix):].split("/")[0] for m in self.members}
                for name in sorted(tops):
                    if name.lower() in USER_DATA:
                        continue
                    p = t / name
                    if p.is_dir():
                        shutil.rmtree(p)
                    elif p.exists():
                        p.unlink()
            self.phase = "解压"
            self.total = self.size
            for m in self.members:
                rel = m.filename[len(self.prefix):]
                if not rel or rel.startswith("/") or ".." in rel.split("/"):
                    raise RuntimeError(f"安装包包含非法路径：{m.filename}")
                if rel.split("/")[0].lower() in USER_DATA:
                    raise RuntimeError(f"安装包不应包含用户数据：{rel}")
                dest = t / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                self.current = rel
                with self.zf.open(m) as src, open(dest, "wb") as out:
                    while True:
                        b = src.read(1 << 20)
                        if not b:
                            break
                        out.write(b)
                        self.done += len(b)
                ts = time.mktime(m.date_time + (0, 0, -1))
                os.utime(dest, (ts, ts))
            self.phase = "校验"
            sums = [ln.split(None, 1) for ln in (t / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
                    if ln.strip()]
            self.total, self.done = len(sums), 0
            for digest, name in sums:
                name = name.strip().lstrip("*")
                self.current = name
                h = hashlib.sha256()
                with open(t / name, "rb") as fh:
                    for b in iter(lambda: fh.read(4 << 20), b""):
                        h.update(b)
                if h.hexdigest() != digest:
                    raise RuntimeError(f"文件校验失败（SHA-256 不一致）：{name}\n安装包可能已损坏，请重新下载。")
                self.done += 1
            self.verified = len(sums)
            self.phase = "完成"
        except BaseException as exc:  # noqa: BLE001
            self.error = str(exc) or type(exc).__name__
        finally:
            self.finished = True
            self.zf.close()


def create_shortcut(target: Path, shortcut_dir: Path | None = None) -> Path:
    """用 Windows 自带的 WScript.Shell 创建桌面快捷方式（不需要管理员权限，不写注册表）。"""
    exe = target / "ESG-XK.exe"
    dest_dir = str(shortcut_dir) if shortcut_dir else "[Environment]::GetFolderPath('Desktop')"
    dest_expr = f"'{dest_dir}'" if shortcut_dir else dest_dir
    ps = (f"$d = {dest_expr}; $s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'ESG-XK.lnk'));"
          f"$s.TargetPath = '{exe}'; $s.WorkingDirectory = '{target}'; $s.IconLocation = '{exe},0';"
          "$s.Description = 'ESG-XK 本地ESG文档知识库'; $s.Save(); Write-Output (Join-Path $d 'ESG-XK.lnk')")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                         text=True, timeout=60, creationflags=0x08000000)
    path = Path((out.stdout or "").strip().splitlines()[-1]) if out.stdout.strip() else None
    if out.returncode != 0 or path is None or not path.exists():
        raise RuntimeError(f"创建快捷方式失败：{(out.stderr or out.stdout)[:200]}")
    return path


_RESULT_FILE: str | None = None


def emit(result: dict) -> None:
    """输出结果（窗口程序没有控制台：测试时写入 --result 文件）。"""
    text = json.dumps(result, ensure_ascii=False)
    if _RESULT_FILE:
        Path(_RESULT_FILE).write_text(text, encoding="utf-8")
    elif sys.stdout is not None:
        print(text)


def launch(target: Path) -> None:
    subprocess.Popen([str(target / "ESG-XK.exe")], cwd=str(target), close_fds=True,
                     creationflags=0x00000008 | 0x00000200)      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


# ---------------------------------------------------------------------- 静默安装（测试）
def silent(args) -> int:
    inst = Installer(payload_path() if not args.payload else Path(args.payload), Path(args.target))
    err = inst.check()
    if err:
        emit({"ok": False, "error": err})
        return 2
    inst.run()
    res = {"ok": not inst.error, "error": inst.error, "upgrade": inst.upgrade, "verified": inst.verified,
           "target": str(inst.target)}
    if not inst.error and not args.no_shortcut:
        res["shortcut"] = str(create_shortcut(inst.target, Path(args.shortcut_dir) if args.shortcut_dir else None))
    emit(res)
    return 0 if res["ok"] else 1


# ---------------------------------------------------------------------- 图形界面
def gui(args) -> int:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QFont, QPainter
    from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                                   QMessageBox, QProgressBar, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

    from esgxk.ui import theme

    app = QApplication(sys.argv[:1])
    app.setFont(QFont(theme.FONT_FAMILY, 10))
    app.setStyleSheet(theme.STYLESHEET)
    app.setWindowIcon(theme.app_icon())

    class Win(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(f"安装 ESG-XK V{APP_VERSION}")
            self.setFixedSize(760, 500)
            self._bg = None
            outer = QVBoxLayout(self)
            outer.setContentsMargins(28, 24, 28, 24)
            card = QFrame()
            card.setObjectName("Card")
            outer.addWidget(card)
            lay = QVBoxLayout(card)
            lay.setContentsMargins(26, 22, 26, 22)
            lay.setSpacing(10)
            head = QLabel("ESG-XK 本地 ESG 文档知识库")
            head.setObjectName("PageTitle")
            sub = QLabel(f"V{APP_VERSION} · 安装程序 · 不上传文件 · 无遥测")
            sub.setObjectName("PageDesc")
            lay.addWidget(head)
            lay.addWidget(sub)
            self.stack = QStackedWidget()
            lay.addWidget(self.stack, 1)
            # 页 1：选项
            p1 = QWidget()
            l1 = QVBoxLayout(p1)
            l1.setContentsMargins(0, 8, 0, 0)
            l1.addWidget(self._muted("程序和它产生的数据都会保存在下面这个文件夹里（便携，不占 C 盘）。"
                                     "不需要管理员权限，不写注册表；以后不想用了，删除这个文件夹即可。"))
            t = QLabel("安装到：")
            t.setObjectName("SectionTitle")
            l1.addWidget(t)
            row = QHBoxLayout()
            self.path = QLineEdit(str(Path(args.target) if args.target else default_target()))
            self.path.textChanged.connect(self._space)
            b = QPushButton("浏览…")
            b.clicked.connect(self._browse)
            row.addWidget(self.path, 1)
            row.addWidget(b)
            l1.addLayout(row)
            self.space = QLabel("")
            self.space.setObjectName("Muted")
            l1.addWidget(self.space)
            self.cb_short = QCheckBox("在桌面创建快捷方式")
            self.cb_short.setChecked(True)
            self.cb_launch = QCheckBox("安装完成后启动 ESG-XK")
            self.cb_launch.setChecked(not args.auto_gui)
            l1.addWidget(self.cb_short)
            l1.addWidget(self.cb_launch)
            l1.addStretch(1)
            self.stack.addWidget(p1)
            # 页 2：进度
            p2 = QWidget()
            l2 = QVBoxLayout(p2)
            l2.addStretch(1)
            self.phase = QLabel("正在安装…")
            self.phase.setObjectName("SectionTitle")
            self.bar = QProgressBar()
            self.bar.setRange(0, 1000)
            self.cur = QLabel("")
            self.cur.setObjectName("Muted")
            l2.addWidget(self.phase)
            l2.addWidget(self.bar)
            l2.addWidget(self.cur)
            l2.addStretch(2)
            self.stack.addWidget(p2)
            # 页 3：完成
            p3 = QWidget()
            l3 = QVBoxLayout(p3)
            self.done_title = QLabel("安装完成")
            self.done_title.setObjectName("SectionTitle")
            self.done_text = QLabel("")
            self.done_text.setWordWrap(True)
            self.done_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            l3.addWidget(self.done_title)
            l3.addWidget(self.done_text)
            l3.addStretch(1)
            self.stack.addWidget(p3)
            btns = QHBoxLayout()
            btns.addStretch(1)
            self.b_cancel = QPushButton("取消")
            self.b_cancel.clicked.connect(self.close)
            self.b_next = QPushButton("安装")
            self.b_next.setObjectName("Primary")
            self.b_next.setMinimumWidth(120)
            self.b_next.clicked.connect(self._next)
            btns.addWidget(self.b_cancel)
            btns.addWidget(self.b_next)
            lay.addLayout(btns)
            self.inst: Installer | None = None
            self.timer = QTimer(self)
            self.timer.setInterval(100)
            self.timer.timeout.connect(self._poll)
            self._space()

        @staticmethod
        def _muted(text: str) -> QLabel:
            lab = QLabel(text)
            lab.setObjectName("Muted")
            lab.setWordWrap(True)
            return lab

        def paintEvent(self, e):  # noqa: N802
            if self._bg is None or self._bg.size() != self.size() * self.devicePixelRatioF():
                self._bg = theme.aurora_pixmap(self.width(), self.height(), self.devicePixelRatioF())
            p = QPainter(self)
            p.drawPixmap(0, 0, self._bg)
            p.end()

        def _browse(self):
            d = QFileDialog.getExistingDirectory(self, "选择安装位置", self.path.text())
            if d:
                self.path.setText(str(normalize_choice(Path(os.path.normpath(d)))))

        def _space(self):
            t = Path(self.path.text().strip() or ".")
            probe = t
            while not probe.exists() and probe.parent != probe:
                probe = probe.parent
            try:
                free = shutil.disk_usage(probe).free
                if not hasattr(self, "_need"):
                    with zipfile.ZipFile(payload_path()) as zf:
                        self._need = sum(i.file_size for i in zf.infolist())
                need = self._need
                upg = "（检测到已安装的 ESG-XK：将更新程序文件，你的数据与模型保持不变）" \
                    if (t / "ESG-XK.exe").exists() else ""
                self.space.setText(f"需要约 {need / 1e6:.0f} MB，目标磁盘可用 {free / 1e9:.1f} GB。{upg}")
            except OSError:
                self.space.setText("")

        def _next(self):
            idx = self.stack.currentIndex()
            if idx == 0:
                self.inst = Installer(payload_path(), Path(self.path.text().strip()))
                err = self.inst.check()
                if err:
                    if args.auto_gui:
                        emit({"ok": False, "error": err})
                        QApplication.quit()
                        return
                    QMessageBox.warning(self, "无法安装", err)
                    return
                self.stack.setCurrentIndex(1)
                self.b_next.setEnabled(False)
                self.b_cancel.setEnabled(False)
                threading.Thread(target=self.inst.run, daemon=True).start()
                self.timer.start()
            elif idx == 2:
                if self.cb_launch.isChecked() and self.inst and not self.inst.error:
                    launch(self.inst.target)
                self.close()

        def _poll(self):
            i = self.inst
            self.phase.setText({"解压": "正在复制程序文件…", "校验": "正在逐个校验文件（SHA-256）…",
                                "移除旧版程序文件": "正在移除旧版程序文件（保留你的数据）…"}.get(i.phase, "正在安装…"))
            if i.total:
                self.bar.setValue(int(i.done * 1000 / max(1, i.total)))
            self.cur.setText(i.current[-90:])
            if not i.finished:
                return
            self.timer.stop()
            self.stack.setCurrentIndex(2)
            self.b_next.setEnabled(True)
            if i.error:
                self.done_title.setText("安装失败")
                self.done_text.setText(i.error)
                self.b_next.setText("关闭")
                self.cb_launch.setChecked(False)
                result = {"ok": False, "error": i.error}
            else:
                extra = ""
                shortcut = None
                if self.cb_short.isChecked():
                    try:
                        shortcut = create_shortcut(i.target, Path(args.shortcut_dir) if args.shortcut_dir else None)
                        extra = "\n桌面快捷方式：ESG-XK"
                    except Exception as exc:  # noqa: BLE001
                        extra = f"\n（{exc}）"
                self.done_text.setText(
                    f"已安装到：{i.target}\n全部 {i.verified} 个文件 SHA-256 校验通过。{extra}\n\n"
                    "第一次打开时，保持默认的“保存在程序文件夹内”即可；需要 OCR / AI 问答时，"
                    "在左侧“模型下载”里一键下载。\n详细步骤见程序文件夹里的“先看这里-使用说明.txt”。\n\n"
                    "卸载：删除整个程序文件夹即可（其中的 ESG-XK-Data 是你的数据，删除前请先备份）。")
                self.b_next.setText("完成")
                result = {"ok": True, "verified": i.verified, "upgrade": i.upgrade, "target": str(i.target),
                          "shortcut": str(shortcut) if shortcut else None}
            if args.auto_gui:
                QTimer.singleShot(600, lambda: self._auto_finish(result))

        def _auto_finish(self, result):
            if args.screens:
                self.grab().save(str(Path(args.screens) / "setup_3_done.png"))
            emit(result)
            QApplication.quit()

    w = Win()
    w.show()
    if args.auto_gui:
        def step1():
            if args.screens:
                Path(args.screens).mkdir(parents=True, exist_ok=True)
                w.grab().save(str(Path(args.screens) / "setup_1_options.png"))
            w._next()
            QTimer.singleShot(700, lambda: args.screens and w.grab().save(str(Path(args.screens) / "setup_2_progress.png")))
        QTimer.singleShot(800, step1)
    return app.exec()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--silent", action="store_true")
    ap.add_argument("--auto-gui", action="store_true")
    ap.add_argument("--target")
    ap.add_argument("--payload")
    ap.add_argument("--shortcut-dir")
    ap.add_argument("--no-shortcut", action="store_true")
    ap.add_argument("--screens")
    ap.add_argument("--result")
    args = ap.parse_args(argv)
    global _RESULT_FILE
    _RESULT_FILE = args.result
    if args.silent:
        if not args.target:
            print("--silent 需要 --target")
            return 2
        return silent(args)
    return gui(args)


if __name__ == "__main__":
    sys.exit(main())
