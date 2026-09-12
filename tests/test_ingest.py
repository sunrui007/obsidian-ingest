import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ingest


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = {"root": self.vault, "attachment_folder": "_attachments/收录"}
        self.work = self.root / "work"
        self.work.mkdir()
        self.body = "正文含中文、反斜线 \\n，不应被误当成换行。\n\n```python\nprint('$HOME')\n```\n\n![样图](<原图.png>)\n"
        (self.work / "extracted.md").write_text(self.body, encoding="utf-8")
        (self.work / "original.png").write_bytes(b"original image fixture")
        self.bundle = {"status": "complete", "source": "https://example.org/article", "source_id": "a" * 64,
                       "source_type": "article", "markdown_file": str(self.work / "extracted.md"),
                       "assets": [{"path": str(self.work / "original.png"), "name": "原图.png"}], "metadata": {}, "warnings": []}
        self.decision = {"title": "测试：知识归档", "category": "技术/新主题", "summary": "简短摘要", "tags": ["归档"]}
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        ingest.json_write(self.work / "bundle.json", self.bundle)
        ingest.json_write(self.work / "decision.json", self.decision)

    def run_import(self, dry_run=False):
        return ingest.import_note(self.config, self.work / "bundle.json", self.work / "decision.json", dry_run)

    def test_creates_category_preserves_content_and_attachment(self):
        result = self.run_import()
        self.assertEqual(result["status"], "imported")
        note = Path(result["path"])
        body = note.read_text(encoding="utf-8")
        self.assertIn("print('$HOME')", body)
        self.assertIn("反斜线 \\n", body)
        self.assertIn("../../_attachments/收录/" + "a" * 16 + "/原图.png", body)
        self.assertIn("source_id: \"" + "a" * 64 + "\"", body)
        copied = self.vault / "_attachments/收录" / ("a" * 16) / "原图.png"
        self.assertEqual(copied.read_bytes(), b"original image fixture")

    def test_dry_run_makes_no_vault_changes(self):
        self.assertEqual(self.run_import(True)["status"], "planned")
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_duplicate_is_not_rewritten(self):
        first = self.run_import()
        before = Path(first["path"]).stat().st_mtime_ns
        second = self.run_import()
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(Path(first["path"]).stat().st_mtime_ns, before)

    def test_same_content_different_source_is_duplicate(self):
        first = self.run_import()
        self.bundle["source_id"] = "b" * 64
        self.bundle["source"] = "https://example.org/mirror"
        self.save()
        self.assertEqual(self.run_import()["path"], first["path"])

    def test_same_title_never_overwrites_legacy_note(self):
        directory = self.vault / "技术/新主题"
        directory.mkdir(parents=True)
        legacy = directory / (ingest.safe_name(self.decision["title"]) + ".md")
        legacy.write_text("用户原笔记", encoding="utf-8")
        result = self.run_import()
        self.assertNotEqual(Path(result["path"]), legacy)
        self.assertEqual(legacy.read_text(encoding="utf-8"), "用户原笔记")

    def test_incomplete_source_is_rejected_without_note(self):
        self.bundle["status"] = "needs_ocr"
        self.save()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.run_import()
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_category_traversal_and_hidden_settings_are_rejected(self):
        for value in ["../outside", "C:/outside", "/tmp", ".obsidian/plugins", "技术/../其他", "技术/CON", "技术/"]:
            self.decision["category"] = value
            self.save()
            with self.subTest(category=value), self.assertRaises(ValueError):
                self.run_import()

    def test_symlink_escape_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (self.vault / "linked").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("System does not permit creating symlinks")
        self.decision["category"] = "linked/child"
        self.save()
        with self.assertRaises(ValueError):
            self.run_import()
        self.assertEqual(list(outside.iterdir()), [])

    def test_lock_prevents_concurrent_mutations(self):
        state = self.vault / "_attachments/.obsidian-ingest"
        state.mkdir(parents=True)
        (state / "import.lock").write_text("active", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Another import"):
            self.run_import()
        self.assertFalse((self.vault / "技术").exists())

    def test_source_identity_keeps_substantive_query(self):
        self.assertEqual(ingest.canonical_url("https://example.org/?from=2020&utm_source=abc"), "https://example.org/?from=2020")
        self.assertNotEqual(ingest.canonical_url("https://www.bilibili.com/video/BVabc?p=1"), ingest.canonical_url("https://www.bilibili.com/video/BVabc?p=2"))
        self.assertEqual(ingest.canonical_url("https://mp.weixin.qq.com/s?__biz=b&mid=1&idx=1&sn=s&scene=2"), "https://mp.weixin.qq.com/s?__biz=b&idx=1&mid=1&sn=s")

    def test_taxonomy_excludes_attachments_and_hidden_folders(self):
        self.run_import()
        (self.vault / ".obsidian").mkdir()
        (self.vault / ".obsidian/x.md").write_text("hidden", encoding="utf-8")
        result = ingest.taxonomy(self.config)
        self.assertEqual(result["markdown_count"], 1)
        self.assertEqual({c["path"] for c in result["categories"]}, {"技术", "技术/新主题"})


if __name__ == "__main__":
    unittest.main()
