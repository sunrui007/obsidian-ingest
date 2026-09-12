import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import extract_media as media
import ingest


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.settings = self.root / "settings"
        self.settings.mkdir()
        self.path = self.settings / "config.json"
        self.other_cwd = self.root / "elsewhere"
        self.other_cwd.mkdir()
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("OBSIDIAN_INGEST_CONFIG", None)
        os.environ.pop("OBSIDIAN_INGEST_MODEL_DIR", None)
        self.save({"vault_path": "../vault"})

    def save(self, value, path=None):
        target = path or self.path
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return target

    def cli(self, *arguments, config=True):
        command = [sys.executable, str(Path(ingest.__file__).resolve())]
        if config:
            command.extend(["--config", str(self.path)])
        return subprocess.run(command + list(arguments), cwd=self.other_cwd,
                              env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                              capture_output=True, text=True, encoding="utf-8", timeout=30)

    def source(self, suffix=".md"):
        source = self.root / ("中文来源" + suffix)
        source.write_text("# 原始标题\n\n可核对的原始正文。", encoding="utf-8")
        return source

    def test_config_selection_explicit_then_environment_then_skill_default(self):
        fake_skill = self.root / "isolated-skill"
        environment_path = self.root / "environment.json"
        with patch.object(ingest, "SKILL", fake_skill):
            self.assertEqual(ingest.config_file(), fake_skill / "config.json")
            os.environ["OBSIDIAN_INGEST_CONFIG"] = str(environment_path)
            self.assertEqual(ingest.config_file(), environment_path)
            self.assertEqual(ingest.config_file(self.path), self.path)

    def test_default_paths_are_resolved_beside_config(self):
        config = ingest.config_load(self.path)
        self.assertEqual(config["attachment_folder"], "_attachments/收录")
        self.assertEqual(config["whisper_model"], "small")
        self.assertEqual(config["root"], self.vault)
        self.assertEqual(config["work_root"], self.settings / ".work")
        self.assertEqual(config["model_root"], self.settings / ".models")
        self.assertEqual(config["config_path"], self.path)
        for name in ("root", "work_root", "model_root", "config_path"):
            self.assertIsInstance(config[name], Path)

    def test_relative_paths_do_not_depend_on_process_working_directory(self):
        self.save({"vault_path": "../vault", "work_dir": "jobs", "model_dir": "../cache"})
        previous = Path.cwd()
        try:
            os.chdir(self.other_cwd)
            config = ingest.config_load(self.path)
        finally:
            os.chdir(previous)
        self.assertEqual(config["root"], self.vault)
        self.assertEqual(config["work_root"], self.settings / "jobs")
        self.assertEqual(config["model_root"], self.root / "cache")

    def test_environment_variables_expand_for_all_directory_settings(self):
        os.environ["OBSIDIAN_CONFIG_TEST_ROOT"] = str(self.root)
        self.save({"vault_path": "${OBSIDIAN_CONFIG_TEST_ROOT}/vault",
                   "work_dir": "${OBSIDIAN_CONFIG_TEST_ROOT}/jobs",
                   "model_dir": "${OBSIDIAN_CONFIG_TEST_ROOT}/models"})
        config = ingest.config_load(self.path)
        self.assertEqual(config["root"], self.vault)
        self.assertEqual(config["work_root"], self.root / "jobs")
        self.assertEqual(config["model_root"], self.root / "models")

    def test_home_expands_for_all_directory_settings(self):
        with patch.dict(os.environ, {"HOME": str(self.root), "USERPROFILE": str(self.root)}):
            self.save({"vault_path": "~/vault", "work_dir": "~/jobs", "model_dir": "~/models"})
            config = ingest.config_load(self.path)
        self.assertEqual(config["root"], self.vault)
        self.assertEqual(config["work_root"], self.root / "jobs")
        self.assertEqual(config["model_root"], self.root / "models")

    def test_legacy_model_environment_overrides_config(self):
        self.save({"vault_path": "../vault", "model_dir": "configured-cache"})
        os.environ["OBSIDIAN_INGEST_MODEL_DIR"] = str(self.root / "legacy-cache")
        self.assertEqual(ingest.config_load(self.path)["model_root"], self.root / "legacy-cache")

    def test_attachment_separator_is_normalized_for_links(self):
        self.save({"vault_path": "../vault", "attachment_folder": "资料附件\\收藏"})
        self.assertEqual(ingest.config_load(self.path)["attachment_folder"], "资料附件/收藏")

    def test_config_fields_reject_empty_and_nonstring_values(self):
        for field in ("vault_path", "attachment_folder", "work_dir", "model_dir", "whisper_model"):
            for value in ("", "   ", None, 123, False, []):
                with self.subTest(field=field, value=value):
                    self.save({"vault_path": "../vault", field: value})
                    with self.assertRaises(ValueError):
                        ingest.config_load(self.path, require_vault=False)

    def test_unsafe_attachment_paths_are_rejected(self):
        for value in ("../outside", "资料/../outside", "/absolute", "C:/outside",
                      "\\\\server\\share", ".obsidian/plugins", "资料//图片", "资料/CON"):
            with self.subTest(path=value):
                self.save({"vault_path": "../vault", "attachment_folder": value})
                with self.assertRaises(ValueError):
                    ingest.config_load(self.path, require_vault=False)

    def test_missing_vault_can_be_loaded_for_configuration_only(self):
        self.save({"vault_path": "../not-on-this-machine"})
        config = ingest.config_load(self.path, require_vault=False)
        self.assertEqual(config["root"], self.root / "not-on-this-machine")
        with self.assertRaises(ValueError):
            ingest.config_load(self.path)
        self.assertFalse(config["root"].exists())

    def test_cli_show_works_with_unavailable_vault_without_creating_it(self):
        self.save({"vault_path": "../not-on-this-machine", "whisper_model": "tiny"})
        before = self.path.read_bytes()
        result = self.cli("config", "show")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not-on-this-machine", result.stdout)
        self.assertIn("tiny", result.stdout)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / "not-on-this-machine").exists())

    def test_cli_partial_update_preserves_other_and_unknown_settings(self):
        before = {"vault_path": "../not-on-this-machine", "attachment_folder": "资料/原件",
                  "work_dir": "jobs", "model_dir": "models", "whisper_model": "small",
                  "future_extension": {"enabled": True, "label": "保留用户设置"}}
        self.save(before)
        result = self.cli("config", "set", "--whisper-model", "tiny")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ingest.json_read(self.path), {**before, "whisper_model": "tiny"})
        self.assertFalse((self.root / "not-on-this-machine").exists())

    def test_cli_can_set_all_fields_for_another_machine(self):
        result = self.cli("config", "set", "--vault-path", "../future-vault",
                          "--attachment-folder", "附件\\原件", "--work-dir", "../future-work",
                          "--model-dir", "../future-models", "--whisper-model", "base")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = ingest.config_load(self.path, require_vault=False)
        self.assertEqual(config["root"], self.root / "future-vault")
        self.assertEqual(config["work_root"], self.root / "future-work")
        self.assertEqual(config["model_root"], self.root / "future-models")
        self.assertEqual(config["attachment_folder"], "附件/原件")
        self.assertEqual(config["whisper_model"], "base")
        self.assertFalse(config["root"].exists())

    def test_invalid_cli_update_leaves_original_file_byte_identical(self):
        original = self.path.read_bytes()
        for value in ("../outside", "C:/outside", ".obsidian/plugins"):
            with self.subTest(path=value):
                result = self.cli("config", "set", "--attachment-folder", value,
                                  "--whisper-model", "tiny")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.path.read_bytes(), original)

    def test_cli_environment_config_is_independent_and_explicit_config_wins(self):
        environment_path = self.save({"vault_path": "not-here", "whisper_model": "tiny"},
                                     self.root / "environment.json")
        os.environ["OBSIDIAN_INGEST_CONFIG"] = str(environment_path)
        original_explicit = self.path.read_bytes()
        result = self.cli("config", "set", "--whisper-model", "base", config=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ingest.json_read(environment_path)["whisper_model"], "base")
        self.assertEqual(self.path.read_bytes(), original_explicit)
        environment_before = environment_path.read_bytes()
        result = self.cli("config", "set", "--whisper-model", "medium")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ingest.json_read(self.path)["whisper_model"], "medium")
        self.assertEqual(environment_path.read_bytes(), environment_before)

    def test_automatic_prepare_directories_are_isolated_under_configured_work_root(self):
        self.save({"vault_path": "../vault", "work_dir": "jobs"})
        config = ingest.config_load(self.path)
        source = self.source()
        first = ingest.prepare(str(source), None, config)
        second = ingest.prepare(str(source), None, config)
        first_bundle, second_bundle = Path(first["bundle"]), Path(second["bundle"])
        self.assertEqual((first["status"], second["status"]), ("complete", "complete"))
        self.assertNotEqual(first_bundle.parent, second_bundle.parent)
        for bundle in (first_bundle, second_bundle):
            self.assertEqual(bundle.parent.parent, config["work_root"])
            self.assertTrue(bundle.is_file())
            self.assertTrue((bundle.parent / "extracted.md").is_file())
        (first_bundle.parent / "extracted.md").write_text("仅修改第一项", encoding="utf-8")
        self.assertIn("可核对的原始正文", (second_bundle.parent / "extracted.md").read_text(encoding="utf-8"))

    def test_cli_prepare_can_omit_work_dir_and_explicit_directory_takes_precedence(self):
        source = self.source()
        result = self.cli("prepare", str(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        automatic = Path(json.loads(result.stdout)["bundle"])
        self.assertEqual(automatic.parent.parent, self.settings / ".work")
        explicit = self.root / "explicit-job"
        result = self.cli("prepare", str(source), "--work-dir", str(explicit))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(json.loads(result.stdout)["bundle"]).parent, explicit)

    def test_configured_attachment_links_and_duplicate_import_preserve_original(self):
        self.save({"vault_path": "../vault", "attachment_folder": "资料附件\\收藏"})
        config = ingest.config_load(self.path)
        original = self.root / "原图.png"
        original.write_bytes(b"unchanged image fixture")
        source = self.source()
        source.write_text("# 图文资料\n\n可核对的正文。\n\n![原图](<原图.png>)", encoding="utf-8")
        prepared = ingest.prepare(str(source), None, config)
        bundle = Path(prepared["bundle"])
        decision = self.save({"title": "图文资料", "category": "技术/归档"},
                             bundle.parent / "decision.json")
        first = ingest.import_note(config, bundle, decision)
        self.assertEqual(first["status"], "imported")
        note = Path(first["path"])
        text = note.read_text(encoding="utf-8")
        self.assertIn("../../资料附件/收藏/", text)
        assets = list((self.vault / "资料附件/收藏").rglob("*"))
        copied_images = [path for path in assets if path.is_file() and path.suffix == ".png"]
        self.assertEqual(len(copied_images), 1)
        self.assertEqual(copied_images[0].read_bytes(), original.read_bytes())
        before = {path.relative_to(self.vault): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.vault.rglob("*") if path.is_file()}
        second = ingest.import_note(config, bundle, decision)
        self.assertEqual(second["status"], "duplicate")
        after = {path.relative_to(self.vault): (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in self.vault.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(original.read_bytes(), b"unchanged image fixture")

    def test_cli_import_requires_existing_vault(self):
        config = ingest.config_load(self.path)
        prepared = ingest.prepare(str(self.source()), None, config)
        decision = self.save({"title": "测试笔记", "category": "技术"}, self.root / "decision.json")
        missing = self.root / "unavailable-vault"
        self.save({"vault_path": str(missing)})
        result = self.cli("import", "--bundle", prepared["bundle"], "--decision", str(decision))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unavailable-vault", result.stderr)
        self.assertFalse(missing.exists())
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_prepare_passes_configured_model_directory_through_media_to_asr(self):
        self.save({"vault_path": "../vault", "model_dir": "configured-models", "whisper_model": "tiny"})
        config = ingest.config_load(self.path)
        original = self.source(".wav")
        calls = []

        def transcribe(path, model, model_dir=None):
            calls.append((Path(path), model, Path(model_dir) if model_dir is not None else None))
            return [(0.0, "这是测试转写正文。")], {"extraction_method": "local_asr"}

        with patch.object(media, "_transcribe", side_effect=transcribe):
            result = ingest.prepare(str(original), None, config)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(calls, [(original, "tiny", self.settings / "configured-models")])
        self.assertFalse((self.settings / "configured-models").exists())


if __name__ == "__main__":
    unittest.main()
