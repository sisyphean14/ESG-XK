"""生成 ESG-XK 测试语料到 D:\\ESG-XK\\ceshi（可重复执行，结果确定）。

目录：
  corpus/    固定语料（测试全程只读，用于源文件完整性校验）
  runctl/    运行控制测试语料（多页扫描 PDF，OCR 负载）
  versions/  版本测试用 v1 / v2 模板（测试时复制到 work/ 再修改）
  work/      测试运行时的可变工作区
  SOURCE_BASELINE.json  corpus + runctl 的 size / mtime / SHA-256 基线
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import random
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"D:\ESG-XK\ceshi")
CORPUS = ROOT / "corpus"
FONT_CJK = r"C:\Windows\Fonts\msyh.ttc"
FONT_HEI = r"C:\Windows\Fonts\simhei.ttf"
random.seed(20260928)

ZH_FACTS = [
    ("1 环境管理", [
        "华东制造基地2025年范围一温室气体排放量为12,345吨二氧化碳当量，较2024年下降6.2%。",
        "范围二温室气体排放量为8,760吨二氧化碳当量，主要来自外购电力。",
        "2025年取水总量为45.6万立方米，单位产值取水量同比下降4.1%。",
        "危险废物全部交由持证单位处置，合规处置率为100%。",
    ]),
    ("2 社会责任", [
        "员工安全培训覆盖率达到98.5%，损失工时事故率（LTIFR）为0.21。",
        "公司全年组织员工培训人均36小时，女性管理者占比为31%。",
        "2025年社区公益投入合计520万元，覆盖乡村教育与环境保护项目。",
    ]),
    ("3 公司治理", [
        "董事会下设ESG委员会，每季度召开一次会议，审议气候风险与可持续发展目标。",
        "供应商ESG审核覆盖率达到85%，对高风险供应商实施现场审核。",
        "公司反腐败培训覆盖全部中高层管理人员，全年未发生重大腐败案件。",
    ]),
]
EN_FACTS = [
    ("1. Climate and Energy", [
        "Scope 1 GHG emissions at the Jakarta plant were 3,210 tCO2e in 2025, a reduction of 4.8% year on year.",
        "Renewable electricity accounted for 37% of total electricity consumption in 2025.",
        "The company commits to reaching net-zero operational emissions by 2050.",
    ]),
    ("2. Water and Waste", [
        "Total water withdrawal decreased by 8.5% compared with the 2024 baseline.",
        "Hazardous waste is treated by licensed contractors and tracked through a digital manifest system.",
    ]),
    ("3. Governance", [
        "Anti-corruption training was completed by 100% of managers during the reporting year.",
        "The Board Sustainability Committee reviews ESG performance twice a year.",
    ]),
]
ID_FACTS = [
    ("1. Lingkungan", [
        "Emisi gas rumah kaca cakupan 1 Pabrik Surabaya pada tahun 2025 sebesar 2.150 ton CO2e.",
        "Limbah B3 dikelola oleh pihak ketiga yang berizin sesuai peraturan pemerintah.",
        "Penggunaan air per unit produksi turun 5 persen dibandingkan tahun sebelumnya.",
    ]),
    ("2. Keselamatan dan Karyawan", [
        "Tingkat kecelakaan kerja (LTIFR) tercatat sebesar 0,35 pada tahun 2025.",
        "Program pelatihan karyawan mencapai rata-rata 24 jam per orang per tahun.",
    ]),
    ("3. Tata Kelola", [
        "Perusahaan menerapkan kebijakan anti korupsi dan sistem pelaporan pelanggaran (whistleblowing).",
        "Seluruh pemasok utama menandatangani kode etik pemasok.",
    ]),
]


def font(size: int, cjk: bool = True) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_CJK if cjk else r"C:\Windows\Fonts\arial.ttf", size)


def text_image(lines: list[str], width: int = 1654, height: int = 2339, size: int = 38, noise: bool = True,
               blur: float = 0.0) -> Image.Image:
    img = Image.new("RGB", (width, height), (252, 252, 250))
    d = ImageDraw.Draw(img)
    f = font(size)
    y = 140
    for ln in lines:
        wrapped = wrap(ln, 36 if any("\u4e00" <= c <= "\u9fff" for c in ln) else 70)
        for w in wrapped:
            d.text((120, y), w, fill=(20, 20, 20), font=f)
            y += int(size * 1.7)
        y += int(size * 0.8)
    if noise:
        for _ in range(1500):
            x, yy = random.randrange(width), random.randrange(height)
            d.point((x, yy), fill=(random.randint(150, 220),) * 3)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    return img


def wrap(s: str, n: int) -> list[str]:
    if " " in s and not any("\u4e00" <= c <= "\u9fff" for c in s):
        out, cur = [], ""
        for w in s.split():
            if len(cur) + len(w) + 1 > n:
                out.append(cur)
                cur = w
            else:
                cur = (cur + " " + w).strip()
        if cur:
            out.append(cur)
        return out
    return [s[i:i + n] for i in range(0, len(s), n)]


def facts_lines(facts) -> list[str]:
    out = []
    for h, paras in facts:
        out.append(h)
        out += paras
    return out


# ---------------------------------------------------------------- PDF
def make_text_pdf(path: Path, title: str, facts, cjk: bool) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    if "Hei" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Hei", FONT_HEI))
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(title)
    fname = "Hei" if cjk else "Helvetica"
    w, h = A4
    for pi, (heading, paras) in enumerate(facts):
        y = h - 72
        if pi == 0:
            c.setFont(fname, 18)
            c.drawString(60, y, title)
            y -= 40
        c.setFont(fname, 14)
        c.drawString(60, y, heading)
        y -= 28
        c.setFont(fname, 11)
        for p in paras:
            for line in wrap(p, 38 if cjk else 90):
                c.drawString(60, y, line)
                y -= 17
            y -= 12
        c.showPage()
    c.save()


def make_scanned_pdf(path: Path, pages: list[list[str]], blur: float = 0.0) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    for lines in pages:
        img = text_image(lines, blur=blur)
        img = img.rotate(random.uniform(-0.6, 0.6), fillcolor=(252, 252, 250))
        buf = io.BytesIO()
        img.convert("L").save(buf, format="JPEG", quality=80)
        buf.seek(0)
        c.drawImage(ImageReader(buf), 0, 0, width=w, height=h)
        c.showPage()
    c.save()


# ---------------------------------------------------------------- Office
def make_docx(path: Path, title: str, facts, table_rows: list[list[str]] | None = None) -> None:
    import docx

    d = docx.Document()
    d.add_heading(title, level=0)
    for heading, paras in facts:
        d.add_heading(heading, level=1)
        for p in paras:
            d.add_paragraph(p)
    if table_rows:
        d.add_heading("附表 / Appendix", level=1)
        t = d.add_table(rows=len(table_rows), cols=len(table_rows[0]))
        for i, row in enumerate(table_rows):
            for j, v in enumerate(row):
                t.cell(i, j).text = v
    d.core_properties.title = title
    d.save(str(path))


def make_xlsx(path: Path) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "排放数据"
    ws.append(["年份", "基地", "范围一排放(tCO2e)", "范围二排放(tCO2e)", "备注"])
    for y, s1, s2 in [(2023, 13900, 9400), (2024, 13160, 9020), (2025, 12345, 8760)]:
        ws.append([y, "华东制造基地", s1, s2, "经第三方核查" if y == 2025 else ""])
    ws2 = wb.create_sheet("Water")
    ws2.append(["Year", "Site", "Water withdrawal (m3)", "Recycled water (%)"])
    for y, v, r in [(2024, 498000, 21.5), (2025, 456000, 27.3)]:
        ws2.append([y, "East China Plant", v, r])
    ws3 = wb.create_sheet("Limbah")
    ws3.append(["Tahun", "Jenis limbah", "Jumlah (ton)", "Pengelola"])
    ws3.append([2025, "Limbah B3", 86.4, "Pihak ketiga berizin"])
    ws3.append([2025, "Limbah non-B3", 1240.0, "Daur ulang"])
    wb.save(str(path))


def make_pptx(path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = "2025 ESG 管理层简报"
    s.placeholders[1].text = "可持续发展部 · 2026年3月"
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "气候目标进展"
    s.placeholders[1].text = ("2025年范围一和范围二排放合计21,105吨二氧化碳当量。\n"
                              "目标：2030年单位产值碳排放较2020年下降45%。")
    s.notes_slide.notes_text_frame.text = "演讲备注：强调2025年光伏装机容量新增12兆瓦。"
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Safety KPIs"
    rows = [["KPI", "2024", "2025"], ["LTIFR", "0.26", "0.21"], ["Training coverage", "96%", "98.5%"]]
    tbl = s.shapes.add_table(len(rows), 3, Inches(1), Inches(1.8), Inches(7), Inches(1.5)).table
    for i, r in enumerate(rows):
        for j, v in enumerate(r):
            tbl.cell(i, j).text = v
    prs.save(str(path))


# ---------------------------------------------------------------- 主流程
def write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)


def md_text(title: str, facts) -> str:
    out = [f"# {title}", ""]
    for h, paras in facts:
        out += [f"## {h}", ""]
        for p in paras:
            out += [p, ""]
    return "\n".join(out)


def plain_text(title: str, facts) -> str:
    out = [title, ""]
    for h, paras in facts:
        out += [h, ""]
        for p in paras:
            out += [p, ""]
    return "\n".join(out)


def long_zh_text() -> str:
    parts = ["华东制造基地可持续发展年度长篇报告", ""]
    for ch in range(1, 61):
        parts += [f"第{ch}章 专题{ch}", ""]
        for p in range(1, 9):
            parts.append(f"本章第{p}节讨论环境、社会与治理议题在第{ch}个业务单元中的落实情况，"
                         f"包括能源管理、排放核算、员工健康与安全、供应链管理及信息披露等方面的做法与改进计划。"
                         f"该单元编号为U{ch:03d}-{p:02d}，年度重点指标已纳入绩效考核体系并接受内部审计。")
            parts.append("")
    parts += ["附录：长文档检索锚点——蓝色海湾生态修复项目共种植红树林3.2公顷。", ""]
    return "\n".join(parts)


def main() -> None:
    if CORPUS.exists():
        shutil.rmtree(CORPUS)
    for sub in ("runctl", "versions"):
        if (ROOT / sub).exists():
            shutil.rmtree(ROOT / sub)
    (ROOT / "work").mkdir(parents=True, exist_ok=True)
    zh, en, idn = CORPUS / "zh", CORPUS / "en", CORPUS / "id"
    for d in (zh, en, idn):
        d.mkdir(parents=True)

    # 文字版 PDF
    make_text_pdf(zh / "华东基地2025可持续发展报告.pdf", "华东制造基地2025年可持续发展报告", ZH_FACTS, True)
    make_text_pdf(en / "Jakarta_Plant_ESG_Report_2025.pdf", "Jakarta Plant ESG Report 2025", EN_FACTS, False)
    make_text_pdf(idn / "Laporan_Keberlanjutan_Surabaya_2025.pdf", "Laporan Keberlanjutan Surabaya 2025", ID_FACTS, False)
    # 扫描版 PDF（无文字层）
    make_scanned_pdf(zh / "扫描件_环境管理制度.pdf", [
        ["环境管理制度（扫描件）", "第一条 各基地应建立温室气体排放台账，每月统计能源消耗。",
         "第二条 危险废物贮存时间不得超过一年，并按规定申报转移联单。"],
        ["第三条 新建项目应开展环境影响评价并落实三同时制度。", "第四条 本制度自2025年1月1日起施行。"],
    ])
    make_scanned_pdf(idn / "Pindaian_Kebijakan_K3.pdf", [
        ["Kebijakan Keselamatan dan Kesehatan Kerja (K3)", "Setiap karyawan wajib menggunakan alat pelindung diri.",
         "Laporan kecelakaan kerja harus disampaikan dalam 24 jam."]])
    make_scanned_pdf(en / "Scanned_Supplier_Code.pdf", [
        ["Supplier Code of Conduct (scanned)", "Suppliers shall prohibit child labour and forced labour.",
         "Suppliers must report environmental incidents within 48 hours."]])
    # Office
    make_docx(zh / "ESG信息披露管理办法.docx", "ESG信息披露管理办法", ZH_FACTS,
              [["指标", "2024", "2025"], ["范围一排放(吨)", "13,160", "12,345"], ["取水(万立方米)", "49.8", "45.6"]])
    make_docx(en / "Sustainability_Policy.docx", "Group Sustainability Policy", EN_FACTS,
              [["Metric", "2024", "2025"], ["Renewable share", "29%", "37%"]])
    make_docx(idn / "Kebijakan_Lingkungan.docx", "Kebijakan Lingkungan Perusahaan", ID_FACTS)
    make_xlsx(zh / "ESG数据汇总表.xlsx")
    make_pptx(zh / "ESG管理层简报.pptx")
    # 文本
    write(zh / "气候风险说明.md", md_text("气候风险说明", ZH_FACTS[:1]))
    write(en / "water_stewardship.txt", plain_text("Water Stewardship Statement", EN_FACTS[1:2]))
    write(idn / "catatan_limbah.txt", plain_text("Catatan Pengelolaan Limbah", ID_FACTS[:1]))
    write(en / "kpi_summary.csv", "metric,year,value,unit\nScope 1 emissions,2025,3210,tCO2e\n"
                                  "Renewable electricity,2025,37,%\nWater withdrawal change,2025,-8.5,%\n")
    # 图片
    imgs = CORPUS / "images"
    imgs.mkdir()
    text_image(["2025年光伏新增装机容量12兆瓦", "年发电量约1,380万千瓦时"], 1400, 500, 44, noise=False).save(imgs / "光伏装机.png")
    text_image(["Employee volunteering hours reached 18,400 in 2025."], 1600, 400, 42, noise=True).save(
        imgs / "volunteering.jpg", quality=88)
    text_image(["Perusahaan menanam 5.000 pohon mangrove di pesisir Gresik."], 1700, 400, 42).save(
        imgs / "mangrove_id.webp", quality=90)
    text_image(["BMP格式测试：能源审计每三年开展一次"], 1300, 360, 40, noise=False).convert("RGB").save(imgs / "能源审计.bmp")
    frames = [text_image([f"TIFF第{i + 1}页：碳盘查边界说明{i + 1}"], 1300, 360, 40, noise=False) for i in range(2)]
    frames[0].save(imgs / "碳盘查边界.tiff", save_all=True, append_images=frames[1:])
    low = text_image(["低质量扫描：废水排放许可证编号 GD-2025-0917"], 1400, 360, 20, noise=True, blur=2.2)
    low = low.resize((500, 130)).resize((1000, 260))
    low.save(imgs / "低质量_废水许可证.jpg", quality=18)
    # 重复 / 同名 / 损坏 / 空 / 超长
    dup = CORPUS / "duplicates"
    dup.mkdir()
    shutil.copyfile(en / "Jakarta_Plant_ESG_Report_2025.pdf", dup / "copy_of_jakarta_report.pdf")
    shutil.copyfile(zh / "气候风险说明.md", dup / "气候风险说明-副本.md")
    same = CORPUS / "same_name"
    write(same / "北区" / "年度总结.txt", "北区年度总结：2025年北区完成节能改造项目7个，节约标准煤1,050吨。\n")
    write(same / "南区" / "年度总结.txt", "南区年度总结：2025年南区雨水回收系统投运，年回收雨水2.4万立方米。\n")
    broken = CORPUS / "broken"
    write(broken / "损坏文件.pdf", b"%PDF-1.7\n" + bytes(random.getrandbits(8) for _ in range(4000)))
    write(broken / "截断文档.docx", (zh / "ESG信息披露管理办法.docx").read_bytes()[:1500])
    write(broken / "fake_sheet.xlsx", bytes(random.getrandbits(8) for _ in range(2048)))
    write(broken / "空文件.txt", b"")
    write(broken / "不支持的格式.xyz", b"unsupported")
    longd = CORPUS / "long"
    write(longd / "超长报告.txt", long_zh_text())
    # 运行控制语料：多页扫描件（真实 OCR 负载）
    rc = ROOT / "runctl"
    rc.mkdir()
    for i in range(1, 7):
        pages = []
        for p in range(1, 4):
            pages.append([f"运行控制测试文档{i} 第{p}页", f"本页记录第{i}号基地第{p}季度的能源消耗与排放数据。",
                          f"季度用电量为{1000 + i * 37 + p * 11}兆瓦时，天然气消耗{200 + i * 5 + p}千立方米。"])
        make_scanned_pdf(rc / f"扫描批次_{i:02d}.pdf", pages)
    # 版本模板
    write(ROOT / "versions" / "v1" / "碳中和路线图.md",
          "# 碳中和路线图\n\n## 目标\n\n公司计划于2060年前实现碳中和。\n\n## 路径\n\n优先提升能源效率。\n")
    write(ROOT / "versions" / "v2" / "碳中和路线图.md",
          "# 碳中和路线图\n\n## 目标\n\n公司计划提前至2050年实现碳中和，2030年前实现碳达峰。\n\n"
          "## 路径\n\n优先提升能源效率，并在2027年前完成全部基地光伏覆盖。\n")
    # 基线
    baseline = {}
    for base in (CORPUS, rc, ROOT / "versions"):
        for p in sorted(base.rglob("*")):
            if p.is_file():
                st = p.stat()
                baseline[str(p)] = {"size": st.st_size, "mtime": st.st_mtime,
                                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
    (ROOT / "SOURCE_BASELINE.json").write_text(json.dumps(baseline, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "README_测试语料.md").write_text(__doc__, encoding="utf-8")
    print(f"generated {len(baseline)} files under {ROOT}")


if __name__ == "__main__":
    main()
