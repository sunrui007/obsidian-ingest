"""Table geometry, actual Chinese OCR, and review gates; only temporary files."""
import importlib.util
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1] / "skills" / "obsidian-ingest"
sys.path.insert(0, str(ROOT / "scripts"))
if (ROOT / ".deps").is_dir():
    sys.path.insert(0, str(ROOT / ".deps"))

from PIL import Image, ImageDraw, ImageFont
import extract_sources as sources
import ingest
from ocr_layout import render_layout

CHINESE_FONT = Path("C:/Windows/Fonts/msyh.ttc")
CANDY_IMAGE = Path(__file__).parent / "fixtures" / "candy-table.png"
CANDY_TABLE = [["", "苹果", "桃子", "西瓜"], ["圆形", "7", "9", "8"], ["五角星", "7", "6", "4"]]


def table_rows(markdown):
    """Read cell values without imposing whitespace or separator formatting."""
    rows = []
    for line in markdown.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if cells and not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            rows.append(cells)
    return rows


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.image = Image.new("RGB", (1200, 1000), "white")
        self.draw = ImageDraw.Draw(self.image)
        self.font = ImageFont.truetype(str(CHINESE_FONT), 30) if CHINESE_FONT.is_file() else ImageFont.load_default(size=30)
        self.rows = []

    def tearDown(self):
        self.temp.cleanup()

    def text(self, x, y, value, confidence=0.99):
        self.draw.text((x, y), value, fill="black", font=self.font)
        left, top, right, bottom = self.draw.textbbox((x, y), value, font=self.font)
        self.rows.append(([[left, top], [right, top], [right, bottom], [left, bottom]], value, confidence))

    def grid(self, values, x=40, y=140, width=220, height=110, borders=True):
        columns, rows = len(values[0]), len(values)
        if borders:
            for column in range(columns + 1):
                line_x = x + column * width
                self.draw.line((line_x, y, line_x, y + rows * height), fill="black", width=3)
            for row in range(rows + 1):
                line_y = y + row * height
                self.draw.line((x, line_y, x + columns * width, line_y), fill="black", width=3)
        for row, cells in enumerate(values):
            for column, value in enumerate(cells):
                for offset, line in enumerate(value.splitlines()):
                    if line:
                        self.text(x + column * width + 18, y + row * height + 16 + offset * 40, line)

    def render(self):
        path = self.root / "layout.png"
        self.image.save(path)
        # Deliberately scramble detector order: geometric order must determine rows.
        result = render_layout(path, list(reversed(self.rows)))
        self.assertIsInstance(result["markdown"], str)
        self.assertIsInstance(result["tables"], int)
        self.assertIsInstance(result["review_required"], bool)
        self.assertIsInstance(result["warnings"], list)
        return result

    def test_grid_preserves_empty_multiline_pipe_and_surrounding_paragraphs(self):
        self.text(40, 40, "Before table")
        self.grid([["Name", "Value", "Note"], ["Key", "", "line one\nline two"], ["Algo", "SM4", "A|B"]])
        self.text(40, 560, "After table")
        result = self.render()
        self.assertEqual(result["tables"], 1)
        rows = table_rows(result["markdown"])
        self.assertEqual(rows[:1], [["Name", "Value", "Note"]])
        self.assertEqual(rows[1][:2], ["Key", ""])
        self.assertRegex(rows[1][2], r"line one\s*<br\s*/?>\s*line two")
        self.assertEqual(rows[2], ["Algo", "SM4", r"A\|B"])
        self.assertEqual(len(rows), 3)
        self.assertLess(result["markdown"].index("Before table"), result["markdown"].index("| Name"))
        self.assertLess(result["markdown"].index("A\\|B"), result["markdown"].index("After table"))

    def test_separate_tables_keep_their_own_rows_and_columns(self):
        for side_by_side in (False, True):
            with self.subTest(side_by_side=side_by_side):
                self.image = Image.new("RGB", (1200, 1000), "white")
                self.draw, self.rows = ImageDraw.Draw(self.image), []
                self.grid([["Left", "Value"], ["A", "11"]], width=200)
                self.grid([["Right", "Count"], ["B", "22"]],
                          x=640 if side_by_side else 40, y=140 if side_by_side else 560, width=200)
                result = self.render()
                self.assertEqual(result["tables"], 2)
                self.assertEqual(table_rows(result["markdown"]), [["Left", "Value"], ["A", "11"], ["Right", "Count"], ["B", "22"]])

    def test_low_confidence_text_is_retained_and_requires_review(self):
        self.grid([["Name", "Value"], ["Maybe", "256"]])
        self.rows[-1] = (self.rows[-1][0], "256", 0.20)
        result = self.render()
        self.assertIn("256", result["markdown"])
        self.assertTrue(result["review_required"])
        self.assertTrue(result["warnings"])

    def test_merged_cells_are_explicit_or_review_fallback_never_plain_grid(self):
        for span in ("colspan", "rowspan"):
            with self.subTest(span=span):
                self.image = Image.new("RGB", (1200, 1000), "white")
                self.draw, self.rows = ImageDraw.Draw(self.image), []
                values = [["Merged", "", "Third"], ["A", "B", "C"], ["D", "E", "F"]] if span == "colspan" else [["Name", "Value", "Note"], ["Merged", "B", "C"], ["", "E", "F"]]
                self.grid(values)
                if span == "colspan":
                    self.draw.line((260, 143, 260, 247), fill="white", width=5)
                else:
                    self.draw.line((43, 360, 257, 360), fill="white", width=5)
                result = self.render()
                self.assertIn("Merged", result["markdown"])
                if re.search(rf'{span}\s*=\s*["\']?2', result["markdown"]):
                    self.assertIn("<table", result["markdown"])
                else:
                    self.assertTrue(result["review_required"])
                    self.assertTrue(result["warnings"])
                    self.assertEqual(table_rows(result["markdown"]), [], "An unresolved merged cell must not become a fabricated rectangular table")

    def test_borderless_columns_are_not_guessed_into_table_cells(self):
        self.grid([["Name", "Value", "Note"], ["Key", "256", "Bits"], ["Algo", "SM4", "Test"]], borders=False)
        result = self.render()
        self.assertEqual(result["tables"], 0)
        self.assertEqual(table_rows(result["markdown"]), [])
        self.assertTrue(result["review_required"])
        self.assertTrue(result["warnings"])
        for value in ("Name", "Value", "Note", "Key", "256", "Bits", "Algo", "SM4", "Test"):
            self.assertIn(value, result["markdown"])

    def test_borderless_table_with_empty_corner_heading_still_requires_review(self):
        # OCR contains 3 / 4 / 4 cells: an empty corner is not a missing row.
        self.grid(CANDY_TABLE, borders=False)
        result = self.render()
        self.assertEqual(result["tables"], 0)
        self.assertEqual(table_rows(result["markdown"]), [])
        self.assertTrue(result["review_required"])
        self.assertTrue(result["warnings"])
        for row in CANDY_TABLE:
            for value in row:
                if value:
                    self.assertIn(value, result["markdown"])
        # Two aligned rows alone still do not establish the three-row pattern.
        self.image = Image.new("RGB", (1200, 1000), "white")
        self.draw, self.rows = ImageDraw.Draw(self.image), []
        self.grid(CANDY_TABLE[:2], borders=False)
        self.assertFalse(self.render()["review_required"])

    def test_ordinary_single_column_text_is_not_a_table(self):
        for y, text in [(60, "Ordinary text begins here."), (140, "Another paragraph."), (220, "Last line with words.")]:
            self.text(40, y, text)
        result = self.render()
        self.assertEqual(result["tables"], 0)
        self.assertFalse(result["review_required"])
        self.assertLess(result["markdown"].index("Ordinary"), result["markdown"].index("Another"))
        self.assertLess(result["markdown"].index("Another"), result["markdown"].index("Last line"))


class RealImageRegressionTests(unittest.TestCase):
    def test_light_gray_one_pixel_grid_with_correct_ocr_boxes(self):
        # Coordinates are measured on the unchanged 675 x 316 source image.
        # Supplying correct text isolates layout from the OCR 9 -> 6 regression.
        rows = []
        for row, cells in enumerate(CANDY_TABLE):
            for column, value in enumerate(cells):
                if not value:
                    continue
                x, y = (22, 186, 349, 512)[column], 145 + row * 31
                right, bottom = x + len(value) * 16, y + 16
                rows.append(([[x, y], [right, y], [right, bottom], [x, bottom]], value, 0.99))
        result = render_layout(CANDY_IMAGE, rows)
        self.assertEqual(result["tables"], 1, result["warnings"])
        self.assertEqual(table_rows(result["markdown"]), CANDY_TABLE)

    @unittest.skipUnless(importlib.util.find_spec("rapidocr_onnxruntime"), "OCR dependency absent")
    def test_actual_small_text_image_preserves_nine_and_requires_visual_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = sources.extract(str(CANDY_IMAGE), Path(temporary) / "staged")
        self.assertEqual(result["status"], "needs_ocr", result["warnings"])
        self.assertTrue(result["metadata"]["layout_review_required"])
        self.assertEqual(table_rows(result["markdown"]), CANDY_TABLE)
        self.assertEqual(table_rows(result["markdown"])[1][2], "9")
        self.assertLess(result["markdown"].index("不同口味"), result["markdown"].index("| 苹果"))
        self.assertLess(result["markdown"].index("| 五角星"), result["markdown"].index("问题是"))


class ReviewGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.markdown = self.root / "extracted.md"
        self.markdown.write_text("Unverified OCR layout", encoding="utf-8")
        self.verified = self.root / "verified.md"
        self.verified.write_text("| 项目 | 数值 | 备注 |\n| --- | --- | --- |\n| 密钥 | 256 | 位 |\n| 算法 | SM4 | 测试 |\n", encoding="utf-8")
        self.bundle_path = self.root / "bundle.json"
        self.bundle = {"source": "fixture.png", "source_id": "a" * 64, "source_type": "png",
                       "status": "needs_ocr", "markdown_file": str(self.markdown), "assets": [],
                       "metadata": {"layout_review_required": True}, "warnings": []}
        ingest.json_write(self.bundle_path, self.bundle)

    def tearDown(self):
        self.temp.cleanup()

    def test_nonvision_completion_cannot_bypass_pending_layout_review(self):
        for method in ("browser", "manual-transcript"):
            with self.subTest(method=method):
                original = self.bundle_path.read_bytes()
                with self.assertRaises(ValueError):
                    ingest.complete(self.bundle_path, self.verified, method)
                self.assertEqual(self.bundle_path.read_bytes(), original)
                self.assertEqual(self.markdown.read_text("utf-8"), "Unverified OCR layout")

    def test_import_rejects_pending_layout_even_if_status_was_set_complete(self):
        self.bundle["status"] = "complete"
        ingest.json_write(self.bundle_path, self.bundle)
        decision = self.root / "decision.json"
        ingest.json_write(decision, {"title": "Fixture", "category": "Tests", "tags": []})
        vault = self.root / "temporary-vault"
        vault.mkdir()
        with self.assertRaises(ValueError):
            ingest.import_note({"root": vault, "attachment_folder": "_attachments/收录"}, self.bundle_path, decision)
        self.assertEqual(list(vault.iterdir()), [])

    @unittest.skipUnless(importlib.util.find_spec("rapidocr_onnxruntime") and CHINESE_FONT.is_file(), "OCR dependency or Chinese fixture font absent")
    def test_actual_chinese_table_ocr_and_vision_completion_state(self):
        image = Image.new("RGB", (1200, 640), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(str(CHINESE_FONT), 56)
        values = [["项目", "数值", "备注"], ["密钥", "256", "位"], ["算法", "SM4", "测试"]]
        for x in (40, 410, 780, 1150):
            draw.line((x, 80, x, 560), fill="black", width=3)
        for y in (80, 240, 400, 560):
            draw.line((40, y, 1150, y), fill="black", width=3)
        for row, cells in enumerate(values):
            for column, value in enumerate(cells):
                draw.text((70 + column * 370, 122 + row * 160), value, font=font, fill="black")
        source = self.root / "chinese-table.png"
        image.save(source)
        result = sources.extract(str(source), self.root / "staged")
        self.assertEqual(result["status"], "needs_ocr", result["warnings"])
        self.assertTrue(result["metadata"]["layout_review_required"])
        self.assertEqual(table_rows(result["markdown"]), values)
        evidence = result["metadata"]["ocr_layout"][0]
        self.assertEqual(evidence["tables"], 1)
        self.assertTrue(ingest.json_read(evidence["evidence_file"])["rows"][0]["box"])
        if importlib.util.find_spec("pymupdf"):
            import pymupdf
            pdf_path = self.root / "scanned-table.pdf"
            with pymupdf.open() as pdf:
                page = pdf.new_page(width=720, height=384)
                page.insert_image(page.rect, filename=str(source))
                pdf.save(pdf_path)
            scanned = sources.extract(str(pdf_path), self.root / "pdf-staged")
            self.assertEqual(scanned["status"], "needs_ocr", scanned["warnings"])
            self.assertTrue(scanned["metadata"]["layout_review_required"])
            self.assertEqual(scanned["metadata"]["pages"][0]["status"], "needs_ocr")
            self.assertEqual(table_rows(scanned["markdown"]), values)
        result["markdown_file"] = str(self.markdown)
        self.markdown.write_text(result["markdown"], encoding="utf-8")
        ingest.json_write(self.bundle_path, result)
        # This known synthetic transcription tests only the state transition.
        # It is not evidence of a host/user visual verification of arbitrary input.
        completed = ingest.complete(self.bundle_path, self.verified, "vision")
        self.assertEqual(completed["status"], "complete")
        saved = ingest.json_read(self.bundle_path)
        self.assertFalse(saved["metadata"].get("layout_review_required"))
        self.assertTrue(saved["metadata"]["layout_verified"])
        self.assertFalse(saved["metadata"]["ocr_layout"][0]["review_required"])
        self.assertEqual(saved["completion_method"], "vision")
        self.assertEqual(self.markdown.read_text("utf-8"), self.verified.read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
