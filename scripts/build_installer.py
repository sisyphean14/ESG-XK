"""生成分发包：D:\\ESG-XK\\jiaofu\\ESG-XK-<版本>-分发包\\

- ESG-XK-<版本>-portable.zip   便携版压缩包（解压即用）
- ESG-XK-<版本>-Setup.exe      单文件安装程序（选择位置 → 解压 → 逐文件 SHA-256 校验 → 桌面快捷方式）
- 校验值-SHA256.txt            以上两个文件的 SHA-256

前提：已运行 scripts/build_release.py 生成交付目录，且交付目录与其 SHA256SUMS 一致。
"""
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from esgxk import APP_VERSION  # noqa: E402

JIAOFU = Path(r"D:\ESG-XK\jiaofu")
RELEASE = JIAOFU / f"ESG-XK-{APP_VERSION}-portable"
OUT = JIAOFU / f"ESG-XK-{APP_VERSION}-分发包"
WORK = ROOT / "build" / "installer"
ZIP_NAME = f"ESG-XK-{APP_VERSION}-portable.zip"
SETUP_NAME = f"ESG-XK-{APP_VERSION}-Setup"


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(4 << 20), b""):
            h.update(b)
    return h.hexdigest()


def verify_release() -> int:
    n = 0
    for line in (RELEASE / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(None, 1)
        if sha(RELEASE / name.strip().lstrip("*")) != digest:
            raise SystemExit(f"交付目录校验失败：{name}")
        n += 1
    for bad in ("portable.flag", "profile", "ESG-XK-Data", "AI-Models"):
        if (RELEASE / bad).exists():
            raise SystemExit(f"交付目录包含用户数据：{bad}")
    return n


def make_zip(dest: Path) -> int:
    files = sorted(p for p in RELEASE.rglob("*") if p.is_file())
    tmp = dest.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            zf.write(p, f"{RELEASE.name}/{p.relative_to(RELEASE).as_posix()}")
    with zipfile.ZipFile(tmp) as zf:                      # 解压测试：CRC 全部通过
        bad = zf.testzip()
        if bad:
            raise SystemExit(f"ZIP 自检失败：{bad}")
    os.replace(tmp, dest)
    return len(files)


def main() -> None:
    n = verify_release()
    print(f"交付目录校验通过：{n} 个文件")
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    zpath = OUT / ZIP_NAME
    nz = make_zip(zpath)
    print(f"ZIP：{zpath}（{nz} 个文件，{zpath.stat().st_size / 1e6:.1f} MB）")
    # 安装程序
    if WORK.exists():
        shutil.rmtree(WORK)
    (WORK / "payload").mkdir(parents=True)
    shutil.copy2(zpath, WORK / "payload" / ZIP_NAME)
    vi = (ROOT / "packaging" / "version_info.txt").read_text(encoding="utf-8")
    vi = (vi.replace("'ESG-XK 本地ESG文档知识库管理系统'", "'ESG-XK 本地ESG文档知识库 安装程序'")
            .replace("StringStruct('InternalName', 'ESG-XK')", f"StringStruct('InternalName', '{SETUP_NAME}')")
            .replace("'ESG-XK.exe'", f"'{SETUP_NAME}.exe'"))
    (WORK / "version_info.txt").write_text(vi, encoding="utf-8")
    env = dict(os.environ, ESGXK_SETUP_PAYLOAD=str(WORK / "payload" / ZIP_NAME), ESGXK_SETUP_NAME=SETUP_NAME)
    subprocess.run([sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "installer" / "ESG-XK-Setup.spec"),
                    "--noconfirm", "--distpath", str(WORK / "dist"), "--workpath", str(WORK / "work")],
                   check=True, env=env, cwd=str(ROOT))
    setup = OUT / f"{SETUP_NAME}.exe"
    shutil.copy2(WORK / "dist" / f"{SETUP_NAME}.exe", setup)
    print(f"Setup：{setup}（{setup.stat().st_size / 1e6:.1f} MB）")
    lines = [f"ESG-XK {APP_VERSION} 分发包 SHA-256", ""]
    for p in (zpath, setup):
        lines.append(f"{sha(p)}  {p.name}")
    lines += ["", f"程序主文件 ESG-XK.exe：{sha(RELEASE / 'ESG-XK.exe')}",
              "两种任选其一：",
              f"  {setup.name} —— 双击安装：选择安装位置，自动校验，并可创建桌面快捷方式（推荐）。",
              f"  {zpath.name} —— 解压到任意可写文件夹（如 D:\\ESG-XK），双击其中的 ESG-XK.exe 即可。",
              "都不需要管理员权限、不写注册表；卸载 = 删除程序文件夹。"]
    (OUT / "校验值-SHA256.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
