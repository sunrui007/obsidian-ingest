import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import extract_media as media


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, name="音频.wav"):
        path = self.root / name
        path.write_bytes(b"test fixture, not a real recording")
        return path

    def fake_ytdlp(self, info, error=None, mutate_cookies=False):
        module = ModuleType("yt_dlp")
        network = ModuleType("yt_dlp.networking")
        network.Request = lambda *args, **kwargs: None
        opened = []

        class YoutubeDL:
            def __init__(instance, params):
                instance.params = params
                opened.append(params)

            def __enter__(instance):
                return instance

            def __exit__(instance, *_):
                if mutate_cookies and instance.params.get("cookiefile"):
                    Path(instance.params["cookiefile"]).write_text("updated session", encoding="utf-8")

            def extract_info(instance, source, download=False):
                if error:
                    raise error
                return info

            def prepare_filename(instance, data):
                return str(self.root / "missing-file")

        module.YoutubeDL = YoutubeDL
        return patch.dict(sys.modules, {"yt_dlp": module, "yt_dlp.networking": network}), opened

    def test_srt_keeps_chinese_text_and_precise_timestamp(self):
        rows = media.parse_subtitles("1\n00:00:01,250 --> 00:00:03,000\n<strong>密码算法</strong>\n需要核验。\n\n2\n01:02:03,005 --> 01:02:05,000\n第二部分。", ".srt")
        self.assertEqual(rows, [(1.25, "密码算法 需要核验。"), (3723.005, "第二部分。")])
        self.assertEqual(media.timestamp(rows[1][0]), "01:02:03.005")
        self.assertEqual(media.timestamp(3599.9996), "01:00:00.000")

    def test_vtt_cue_settings_html_and_repeated_caption(self):
        text = "WEBVTT\n\nfirst\n00:01.200 --> 00:03.000 align:start\n<c>第一句 &amp; 注释</c>\n\n00:02.000 --> 00:04.000\n第一句 &amp; 注释\n\n00:05.000 --> 00:06.000\n第二句"
        self.assertEqual(media.parse_subtitles(text, "vtt"), [(1.2, "第一句 & 注释"), (5.0, "第二句")])

    def test_bilibili_and_json3_subtitles(self):
        bilibili = json.dumps({"body": [{"from": 1.5, "to": 3, "content": "国密标准"}]})
        youtube = json.dumps({"events": [{"tStartMs": 2300, "segs": [{"utf8": "Hello "}, {"utf8": "world"}]}, {"tStartMs": 5000}]})
        self.assertEqual(media.parse_subtitles(bilibili, "json"), [(1.5, "国密标准")])
        self.assertEqual(media.parse_subtitles(youtube, "json3"), [(2.3, "Hello world")])

    def test_sidecar_precedes_asr_and_preserves_absolute_original(self):
        original = self.source()
        original.with_suffix(".srt").write_text("1\n00:00:01,000 --> 00:00:02,000\n正文来源是字幕。", encoding="utf-8")
        with patch.object(media, "_transcribe") as transcribe:
            result = media.extract(str(original), self.root / "work")
        transcribe.assert_not_called()
        self.assertEqual(result["status"], "complete")
        self.assertIn("[00:00:01.000] 正文来源是字幕。", result["markdown"])
        self.assertEqual(result["assets"][0], {"path": str(original.resolve()), "name": original.name})
        self.assertTrue(all(Path(asset["path"]).is_file() for asset in result["assets"]))

    def test_missing_source_is_blocked_without_empty_note(self):
        result = media.extract(str(self.root / "missing.mp4"), self.root / "work")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["markdown"], "")
        self.assertEqual(result["assets"], [])

    def test_silence_never_succeeds_from_filename(self):
        original = self.source("标题写满知识点.wav")
        with patch.object(media, "_transcribe", return_value=([], {"duration": 2})):
            result = media.extract(str(original), self.root / "work")
        self.assertEqual(result["status"], "needs_transcript")
        self.assertEqual(result["markdown"], "")
        self.assertTrue(result["warnings"])

    def test_model_failure_has_recovery_and_retains_original(self):
        with patch.object(media, "_transcribe", side_effect=OSError("model download unavailable")):
            result = media.extract(str(self.source()), self.root / "work")
        self.assertEqual(result["status"], "needs_transcript")
        self.assertIn("OBSIDIAN_INGEST_MODEL_DIR", " ".join(result["warnings"]))
        self.assertEqual(len(result["assets"]), 1)

    def test_silent_video_keeps_visual_evidence_without_success(self):
        frame = self.root / "frame-01.jpg"
        frame.write_bytes(b"fake JPEG for contract test")
        with patch.object(media, "_transcribe", return_value=([], {})), \
                patch.object(media, "_keyframes", return_value=[(1.25, frame)]):
            result = media.extract(str(self.source("silent.mp4")), self.root / "work")
        self.assertEqual(result["status"], "needs_transcript")
        self.assertTrue(result["metadata"]["visual_review_required"])
        self.assertIn("![关键帧 00:00:01.250](frame-01.jpg)", result["markdown"])
        self.assertEqual(len(result["assets"]), 2)

    def test_remote_real_subtitles_precede_download_and_ignore_description(self):
        info = {"title": "原标题", "description": "不能被当作正文的描述", "subtitles": {"en": [{"ext": "srt", "data": "1\n00:00:01,000 --> 00:00:02,000\nEnglish"}], "zh-CN": [{"ext": "json", "data": '{"body":[{"from":1,"content":"真正的中文字幕"}]}'}]}}
        fake, opened = self.fake_ytdlp(info)
        with fake, patch.object(media, "_remote_audio") as download, patch.object(media, "_transcribe") as asr:
            result = media.extract("https://www.bilibili.com/video/example", self.root / "work")
        download.assert_not_called()
        asr.assert_not_called()
        self.assertEqual(result["status"], "complete")
        self.assertIn("真正的中文字幕", result["markdown"])
        self.assertNotIn(info["description"], result["markdown"])
        self.assertNotIn("cookiesfrombrowser", opened[0])

    def test_remote_login_failure_is_explicitly_blocked(self):
        fake, opened = self.fake_ytdlp({}, error=RuntimeError("HTTP 403: login required"))
        with fake:
            result = media.extract("https://www.douyin.com/video/example", self.root / "work")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["markdown"], "")
        self.assertIn("HTTP 403", " ".join(result["warnings"]))
        self.assertNotIn("cookiefile", opened[0])

    def test_explicit_cookie_file_is_not_changed_or_kept_in_work(self):
        cookie = self.root / "authorized-cookies.txt"
        cookie.write_text("original cookie content", encoding="utf-8")
        info = {"subtitles": {"zh": [{"ext": "srt", "data": "1\n00:00:01,000 --> 00:00:02,000\n真实内容"}]}}
        fake, opened = self.fake_ytdlp(info, mutate_cookies=True)
        with fake:
            result = media.extract("https://www.bilibili.com/video/example", self.root / "work", cookies_file=str(cookie))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(cookie.read_text(encoding="utf-8"), "original cookie content")
        self.assertNotEqual(opened[0]["cookiefile"], str(cookie))
        self.assertFalse(Path(opened[0]["cookiefile"]).exists())

    def test_remote_audio_uses_fresh_audio_only_selector(self):
        output = self.source("downloaded.m4a")
        info = {"formats": [{"format_id": "video", "url": "https://example.com/video", "vcodec": "h264", "acodec": "aac", "height": 720}, {"format_id": "audio", "url": "https://example.com/audio", "vcodec": "none", "acodec": "aac"}], "webpage_url": "https://example.com/watch"}
        fake, opened = self.fake_ytdlp({"requested_downloads": [{"filepath": str(output)}]})
        with fake:
            actual = media._remote_audio(SimpleNamespace(params={"quiet": True}), info, self.root)
        self.assertEqual(actual, output)
        self.assertEqual(opened[0]["format"], "audio")
        self.assertNotIn("video", opened[0]["outtmpl"])

    def test_remote_silent_video_requests_visual_review(self):
        info = {"formats": [{"url": "https://example.com/silent.mp4", "vcodec": "h264", "acodec": "none"}]}
        fake, _ = self.fake_ytdlp(info)
        with fake, patch.object(media, "_keyframes", return_value=[]), patch.object(media, "_transcribe") as asr:
            result = media.extract("https://example.com/watch", self.root / "work")
        asr.assert_not_called()
        self.assertEqual(result["status"], "needs_transcript")
        self.assertTrue(result["metadata"]["visual_review_required"])

    def test_transcribe_download_cache_and_lazy_segments(self):
        module = ModuleType("faster_whisper")
        observed = {}

        class WhisperModel:
            def __init__(instance, model, **kwargs):
                observed.update(kwargs)

            def transcribe(instance, path, **kwargs):
                observed.update(kwargs)
                return iter([SimpleNamespace(start=1.1, text=" 中文转写 ")]), SimpleNamespace(language="zh", duration=2.5)

        module.WhisperModel = WhisperModel
        cache = self.root / "models"
        with patch.dict(sys.modules, {"faster_whisper": module}), patch.dict(os.environ, {"OBSIDIAN_INGEST_MODEL_DIR": str(cache)}):
            rows, metadata = media._transcribe(self.source(), "small")
        self.assertEqual(rows, [(1.1, "中文转写")])
        self.assertEqual(observed["download_root"], str(cache))
        self.assertEqual(observed["device"], "cpu")
        self.assertTrue(observed["vad_filter"])
        self.assertEqual(metadata["language"], "zh")

    @unittest.skipUnless(media._ffmpeg(), "ffmpeg/imageio-ffmpeg is not installed")
    def test_real_video_produces_four_readable_keyframes(self):
        original = self.root / "fixture.mp4"
        subprocess.run([media._ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "testsrc2=size=320x180:rate=10:duration=2", "-c:v", "libx264", str(original)],
                       check=True, capture_output=True, timeout=30)
        frames = media._keyframes(str(original), self.root)
        self.assertEqual(len(frames), 4)
        self.assertEqual([round(start, 2) for start, _ in frames], [0.2, 0.7, 1.2, 1.7])
        for _, path in frames:
            self.assertTrue(path.read_bytes().startswith(b"\xff\xd8"))


if __name__ == "__main__":
    unittest.main()
