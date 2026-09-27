"""Regression coverage uses conspicuously labelled synthetic inputs, no real jobs."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

from app.parsing import extract_file, merge_texts, parse_job, salary_group
from app.exports import export_resume, resume_blocks


SAMPLE = {
    "name": "测试人员（样例）", "headline": "计算机视觉算法工程师",
    "contact": "test@example.invalid", "summary": "以下内容仅为导出功能测试样例。",
    "skills": ["Python", "图像分割"],
    "projects": [{"id": "sample-1", "title": "示例分割项目", "context": "测试背景：医学影像",
                  "bullets": ["使用测试数据验证模型训练流程。", "保留真实项目背景，不添加未提供的效果指标。"]}],
    "education": [], "experience": [], "language": "zh",
}


class ParsingExportsTests(unittest.TestCase):
    def test_salary_ranges_and_annual_packages(self):
        value = salary_group("30–60K·14薪")
        self.assertEqual(value["group"], "可能符合")
        self.assertEqual((value["min_monthly"], value["max_monthly"], value["extra_months"]), (30000, 60000, 2))
        self.assertEqual(salary_group("40-60K·16薪")["group"], "明确符合")
        self.assertEqual(salary_group("30-39K")["group"], "低于目标")
        for value in ["60-90万/年", "年包80万", "面议", "综合薪资40-60K含绩效", "税后40K", "40-60万", "$40-60K"]:
            with self.subTest(value=value):
                parsed = salary_group(value)
                self.assertEqual(parsed["group"], "信息不足")
                self.assertIsNone(parsed["min_monthly"])
        self.assertEqual(salary_group("固定月薪4-6万，另有奖金")["group"], "明确符合")

    def test_salary_base_bonus_bounds_and_ambiguous_units(self):
        for raw in ("4-6万", "底薪20万/年", "奖金40K", "40-60K含绩效", "30-60K·14薪（含绩效）", "40-60K·14薪（含绩效）", "税后月薪40K"):
            with self.subTest(raw=raw):
                self.assertEqual(salary_group(raw)["group"], "信息不足")
        for raw in ("底薪20K+奖金30K", "基本工资20K + 绩效20K"):
            with self.subTest(raw=raw):
                result = salary_group(raw)
                self.assertEqual(result["group"], "低于目标")
                self.assertEqual((result["min_monthly"], result["max_monthly"]), (20000, 20000))
        for raw in ("40K以下", "最高40K", "40K以下+奖金", "最高40-60K"):
            with self.subTest(raw=raw):
                self.assertNotEqual(salary_group(raw)["group"], "明确符合")
        self.assertEqual(salary_group("低于40K")["group"], "低于目标")
        self.assertEqual(salary_group("40K+")["group"], "明确符合")
        self.assertEqual(salary_group("40 - 60K")["group"], "明确符合")
        self.assertEqual(salary_group("月薪4-6万")["group"], "明确符合")

    def test_structured_monthly_cny_from_jsonld_source(self):
        from app.sources import _salary
        raw = _salary({"currency": "CNY", "value": {"minValue": 40000, "maxValue": 60000, "unitText": "MONTH"}})
        self.assertEqual(raw, "CNY 40000–60000 / MONTH")
        for raw in (raw, "RMB 40000–60000 monthly", "CNY 40,000-60,000 per month", "RMB 40000 MONTH"):
            with self.subTest(raw=raw):
                result = salary_group(raw)
                self.assertEqual(result["group"], "明确符合")
                self.assertEqual(result["min_monthly"], 40000)
                self.assertIn(result["max_monthly"], (40000, 60000))
        self.assertEqual(salary_group("CNY 30000–60000 / MONTH")["group"], "可能符合")
        for raw in ("USD 40000–60000 / MONTH", "CNY 40000–60000 / YEAR", "CNY 40-60K / YEAR",
                    "RMB 40000–60000 annual", "CNY 40000–60000 MONTH annual", "CNY 40000–60000",
                    "40000–60000 / MONTH", "CNY ?–60000 / MONTH", "CNY 40000–60000 / HOUR"):
            with self.subTest(raw=raw):
                result = salary_group(raw)
                self.assertEqual(result["group"], "信息不足")
                self.assertIsNone(result["min_monthly"])

    def test_overlap_deduplicates_but_preserves_conflicts(self):
        first = "岗位：算法工程师\n薪资：30-60K\n职责：模型训练\n熟悉 Python"
        second = "职责：模型训练\n熟悉Python\n要求：图像分割经验\n薪资：40-60K"
        merged = merge_texts([first, second])
        self.assertEqual(merged.count("职责：模型训练") + merged.count("职责:模型训练"), 1)
        self.assertIn("30-60K", merged)
        self.assertIn("40-60K", merged)
        self.assertIn("图像分割", merged)

    def test_overlap_preserves_sign_decimal_percent_and_language_symbols(self):
        facts = ["变化 -10%", "变化 10%", "薪资 3.5万", "薪资 35万", "技能 C++", "技能 C#", "范围 30-60K", "范围 3060K", "时间12:30", "时间1230"]
        self.assertEqual(len(merge_texts(["\n".join(facts)]).splitlines()), len(facts))

    def test_parse_unknowns_and_editable_fields(self):
        text = "公司：示例公司\n岗位：视觉算法工程师\n城市：深圳\n薪资：30-60K·14薪\n岗位职责\n开发分割模型\n任职要求\n熟悉Python"
        parsed = parse_job(text)
        self.assertEqual(parsed["company"], "示例公司")
        self.assertEqual(parsed["title"], "视觉算法工程师")
        self.assertEqual(parsed["published_at"], "")
        self.assertEqual(parsed["status_validity"], "未知")
        self.assertIn("published_at", parsed["uncertain_fields"])
        self.assertEqual(parsed["responsibilities"], "开发分割模型")
        # The API receives this ordinary structured dictionary for user correction.
        parsed["title"] = "用户核对后的岗位"
        self.assertEqual(parsed["title"], "用户核对后的岗位")

    def test_publication_not_inferred_from_today(self):
        self.assertEqual(parse_job("今天首次发现 视觉算法工程师")["published_at"], "")
        self.assertEqual(parse_job("发布时间：2026-09-20")["published_at"], "2026-09-20")

    def test_missing_experience_cannot_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                export_resume({"name": "测试", "skills": ["学习过VLM"]}, "docx", Path(tmp) / "blocked.docx")
            self.assertFalse((Path(tmp) / "blocked.docx").exists())

    def test_exports_keep_source_and_preview_content(self):
        from docx import Document
        from pypdf import PdfReader
        from lxml import etree
        initial = copy.deepcopy(SAMPLE)
        with tempfile.TemporaryDirectory() as tmp:
            docx = export_resume(SAMPLE, "docx", Path(tmp) / "sample.docx")
            pdf = export_resume(SAMPLE, "pdf", Path(tmp) / "sample.pdf")
            expected = [block["text"] for block in resume_blocks(SAMPLE)]
            actual_docx = "\n".join(p.text for p in Document(docx).paragraphs)
            actual_pdf = "\n".join(page.extract_text() for page in PdfReader(pdf).pages)
            for value in expected:
                self.assertIn(value, actual_docx)
                self.assertIn(value.replace("\n", ""), actual_pdf.replace("\n", ""))
            with zipfile.ZipFile(docx) as archive:
                root = etree.fromstring(archive.read("word/document.xml"))
                colors = root.xpath("//w:color/@w:val", namespaces={"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"})
                self.assertTrue(colors)
                self.assertTrue(all(color == "000000" for color in colors))
            self.assertEqual(SAMPLE, initial)
            self.assertIn("示例分割项目", extract_file(docx)["text"])
            self.assertIn("示例分割项目", extract_file(pdf)["text"])

    def test_text_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "job.txt"
            file.write_text("测试岗位：算法工程师", encoding="utf-8-sig")
            self.assertEqual(extract_file(file)["text"], "测试岗位：算法工程师")
            file.write_bytes("测试岗位：算法工程师".encode("gb18030"))
            self.assertEqual(extract_file(file)["text"], "测试岗位：算法工程师")

    def test_file_and_docx_expansion_limits(self):
        from app.parsing import MAX_FILE_BYTES
        with tempfile.TemporaryDirectory() as tmp:
            oversized = Path(tmp) / "oversized.txt"
            with oversized.open("wb") as stream:
                stream.truncate(MAX_FILE_BYTES + 1)
            result = extract_file(oversized)
            self.assertEqual(result["method"], "unavailable")
            self.assertIn("12 MB", "".join(result["warnings"]))
            archive = Path(tmp) / "bomb.docx"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
                package.writestr("word/document.xml", "x" * (2 * 1024 * 1024))
            result = extract_file(archive)
            self.assertEqual(result["method"], "unavailable")
            self.assertIn("压缩比", "".join(result["warnings"]))

    def test_pdf_page_and_decompression_limits(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, NameObject
        from app.parsing import MAX_PDF_STREAM_BYTES
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "many-pages.pdf"
            writer = PdfWriter()
            for _ in range(101):
                writer.add_blank_page(width=100, height=100)
            writer.write(pdf)
            result = extract_file(pdf)
            self.assertEqual(result["method"], "unavailable")
            self.assertIn("100 页", "".join(result["warnings"]))
            writer = PdfWriter()
            page = writer.add_blank_page(width=100, height=100)
            content = DecodedStreamObject()
            content.set_data(b" " * (MAX_PDF_STREAM_BYTES + 1024))
            page[NameObject("/Contents")] = writer._add_object(content.flate_encode())
            bomb = Path(tmp) / "compressed-bomb.pdf"
            writer.write(bomb)
            self.assertLess(bomb.stat().st_size, 100_000)
            result = extract_file(bomb)
            self.assertEqual(result["text"], "")
            self.assertIn(result["method"], ("failed", "unavailable"))

    def test_image_pixel_limit_before_decode(self):
        from PIL import Image
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oversized.png"
            Image.new("1", (6500, 6500)).save(path)
            with patch("PIL.ImageOps.exif_transpose", side_effect=AssertionError("must not decode")):
                result = extract_file(path)
            self.assertEqual(result["method"], "unavailable")
            self.assertIn("4000 万像素", "".join(result["warnings"]))

    @unittest.skipUnless(os.name == "nt", "Windows OCR integration requires Windows")
    def test_two_real_overlapping_screenshot_ocr(self):
        from PIL import Image, ImageDraw, ImageFont
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 34)
        pages = [
            ["公司：示例公司", "岗位：视觉算法工程师", "城市：深圳", "薪资：30-60K", "岗位职责", "负责模型训练", "负责图像分割"],
            ["岗位职责", "负责模型训练", "负责图像分割", "任职要求", "熟悉 Python", "有模型部署经验"],
        ]
        results = []
        with tempfile.TemporaryDirectory() as tmp:
            for i, lines in enumerate(pages):
                picture = Image.new("RGB", (1000, 620), "white")
                draw = ImageDraw.Draw(picture)
                for row, line in enumerate(lines):
                    draw.text((40, 30 + row * 75), line, font=font, fill="black")
                path = Path(tmp) / f"screenshot-{i}.png"
                picture.save(path)
                result = extract_file(path)
                self.assertEqual(result["method"], "windows-ocr", result["warnings"])
                results.append(result["text"])
        merged = merge_texts(results)
        compact = merged.replace(" ", "")
        self.assertEqual(compact.count("负责模型训练"), 1, merged)
        self.assertEqual(compact.count("负责图像分割"), 1, merged)
        self.assertIn("有模型部署经验", compact)
        self.assertIn("30-60K", compact)
        job = parse_job(merged)
        self.assertEqual(job["company"], "示例公司")
        self.assertEqual(job["title"], "视觉算法工程师")
        self.assertEqual(salary_group(job["salary_raw"])["group"], "可能符合")
        self.assertIn("模型部署", job["requirements"])


if __name__ == "__main__":
    unittest.main()
