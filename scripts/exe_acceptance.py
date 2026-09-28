"""EXE 真实验收：以独立 Profile / Data Root 启动正式 ESG-XK.exe --acceptance，等待其自动完成并退出。

首次运行对话框（数据目录）由 EXE 自身处理并创建数据目录；AI 资产由 EXE 自身探测。
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil

EXE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"D:\ESG-XK\jiaofu\ESG-XK-1.0.0-portable\ESG-XK.exe")
BASE = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(r"D:\ESG-XK\ceshi\work\exe_accept")
CORPUS = Path(r"D:\ESG-XK\ceshi\corpus")
RUNCTL = Path(r"D:\ESG-XK\ceshi\runctl")

if BASE.exists():
    shutil.rmtree(BASE)
accept_corpus = BASE / "gui_accept"
for sub in ("zh", "en", "id", "images", "duplicates", "broken"):
    shutil.copytree(CORPUS / sub, accept_corpus / sub)
for f in sorted(RUNCTL.glob("*.pdf"))[:3]:
    shutil.copy2(f, accept_corpus / "runctl" / f.name) if (accept_corpus / "runctl").exists() else (
        (accept_corpus / "runctl").mkdir(parents=True), shutil.copy2(f, accept_corpus / "runctl" / f.name))
report = BASE / "gui_acceptance.json"
env = dict(os.environ)
env.update({"ESGXK_PROFILE_DIR": str(BASE / "profile"), "ESGXK_DEFAULT_DATA_ROOT": str(BASE / "ESG-XK-Data"),
            "ESGXK_ACCEPT_CORPUS": str(accept_corpus)})
t0 = time.time()
proc = subprocess.Popen([str(EXE), "--acceptance", str(report)], env=env)
print("started pid", proc.pid, flush=True)
children_seen = set()
while proc.poll() is None:
    try:
        for c in psutil.Process(proc.pid).children(recursive=True):
            children_seen.add((c.pid, c.name(), c.create_time()))
    except psutil.Error:
        pass
    if time.time() - t0 > 3600:
        proc.kill()
        print("TIMEOUT")
        break
    time.sleep(2)
rc = proc.wait()
time.sleep(2)
def _alive(pid, ct):
    try:
        return abs(psutil.Process(pid).create_time() - ct) < 1.0
    except psutil.Error:
        return False


left = [(p, n) for p, n, ct in children_seen if _alive(p, ct)]  # 校验 create_time，排除 PID 复用
data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
summary = {"exit_code": rc, "seconds": round(time.time() - t0), "report": str(report),
           "summary": data.get("summary"), "children_seen": sorted({n for _, n, _c in children_seen}),
           "children_left_after_exit": left,
           "last_shutdown": json.loads((BASE / "ESG-XK-Data" / "runs" / "last_shutdown.json").read_text(encoding="utf-8"))
           if (BASE / "ESG-XK-Data" / "runs" / "last_shutdown.json").exists() else None}
(BASE / "exe_acceptance_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=1))
for r in data.get("results", []):
    print(r["result"], r["step"], "|", (r.get("detail") or "")[:220])
