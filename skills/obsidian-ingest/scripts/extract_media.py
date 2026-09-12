"""Extract real subtitles/speech and visual evidence; never synthesize from titles."""
from __future__ import annotations

import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlparse


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".m4v", ".wmv", ".ts"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
SUBTITLE_EXTENSIONS = {".vtt", ".srt", ".json3", ".json"}
_CUE = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})\s*-->")


def timestamp(seconds: float) -> str:
    millis = max(0, round(float(seconds) * 1000))
    whole, fraction = divmod(millis, 1000)
    hours, whole = divmod(whole, 3600)
    minutes, seconds = divmod(whole, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{fraction:03}"


def parse_subtitles(content: str, extension: str = "") -> list[tuple[float, str]]:
    """Read WebVTT, SRT, YouTube JSON3 and Bilibili subtitle JSON."""
    rows = []
    if extension.lower().lstrip(".") in {"json", "json3"} or content.lstrip().startswith("{"):
        data = json.loads(content)
        if "body" in data:
            rows = [(float(item.get("from", 0)), item.get("content", "")) for item in data["body"]]
        else:
            rows = [(float(item.get("tStartMs", 0)) / 1000,
                     "".join(segment.get("utf8", "") for segment in item.get("segs", [])))
                    for item in data.get("events", [])]
    else:
        for block in re.split(r"\n\s*\n", content.replace("\r\n", "\n")):
            match = _CUE.search(block)
            if match:
                hours, minutes, seconds, millis = (int(value or 0) for value in match.groups())
                start = hours * 3600 + minutes * 60 + seconds + millis / 1000
                text = block[match.end():].partition("\n")[2]
                rows.append((start, text))
    cleaned = []
    for start, value in rows:
        value = html.unescape(re.sub(r"<[^>]*>", "", value))
        value = re.sub(r"\s+", " ", value).strip()
        if value and start >= 0 and (not cleaned or value != cleaned[-1][1]):
            cleaned.append((start, value))
    return cleaned


def _brief(error: Exception) -> str:
    message = re.sub(r"https?://\S+", "[远端地址]", str(error))
    message = re.sub(r"(?i)(cookie|token|authorization)\s*[:=]\s*\S+", r"\1=[已隐藏]", message)
    return f"{type(error).__name__}: {message[:220]}"


def _ffmpeg() -> str | None:
    available = shutil.which("ffmpeg")
    if available:
        return available
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, AttributeError):
        return None


def _input_args(source: str, headers: dict | None = None) -> list[str]:
    args = []
    if source.startswith(("https://", "http://")):
        args += ["-rw_timeout", "20000000"]
        if headers:
            safe = [(str(k), str(v)) for k, v in headers.items()
                    if "\r" not in str(k) + str(v) and "\n" not in str(k) + str(v)]
            args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in safe)]
    return args + ["-i", source]


def _keyframes(source: str, directory: Path, *, duration: float | None = None,
               headers: dict | None = None) -> list[tuple[float, Path]]:
    executable = _ffmpeg()
    if not executable:
        return []
    if not duration:
        probe = subprocess.run([executable, "-hide_banner", *_input_args(source, headers)],
                               capture_output=True, timeout=40)
        found = re.search(rb"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", probe.stderr)
        if not found:
            return []
        hours, minutes, seconds = (float(value) for value in found.groups())
        duration = hours * 3600 + minutes * 60 + seconds
    result = []
    for index, fraction in enumerate((.10, .35, .60, .85), 1):
        position = duration * fraction
        target = directory / f"frame-{index:02}.jpg"
        command = [executable, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(position),
                   *_input_args(source, headers), "-frames:v", "1", "-vf", "scale=1280:-2", str(target)]
        operation = subprocess.run(command, capture_output=True, timeout=55)
        if operation.returncode == 0 and target.is_file() and target.stat().st_size:
            result.append((position, target))
    return result


def _transcribe(path: Path, model: str, model_dir: Path | None = None) -> tuple[list[tuple[float, str]], dict]:
    from faster_whisper import WhisperModel
    cache = Path(model_dir or os.environ.get("OBSIDIAN_INGEST_MODEL_DIR") or Path(__file__).resolve().parents[1] / ".models").expanduser().resolve()
    cache.mkdir(parents=True, exist_ok=True)
    print(f"正在加载本地语音模型 {model}（首次运行会下载模型），随后转写音轨。", file=sys.stderr)
    recognizer = WhisperModel(model, device="cpu", compute_type="int8", download_root=str(cache))
    segments, info = recognizer.transcribe(str(path), beam_size=5, vad_filter=True,
                                           condition_on_previous_text=False)
    rows = [(float(segment.start), segment.text.strip()) for segment in segments if segment.text.strip()]
    return rows, {"language": info.language, "duration": info.duration,
                  "transcription_model": model, "extraction_method": "local_asr"}


class _QuietLogger:
    def debug(self, message):
        pass

    info = debug
    warning = debug
    error = debug


def _language_order(language: str) -> tuple[int, str]:
    language = language.lower()
    return (0 if language.startswith(("zh", "cmn")) else 1 if language.startswith("en") else 2, language)


def _remote_subtitles(ydl, info: dict) -> tuple[list[tuple[float, str]], dict]:
    from yt_dlp.networking import Request
    for kind in ("subtitles", "automatic_captions"):
        languages = info.get(kind) or {}
        for language in sorted(languages, key=_language_order):
            if language == "live_chat":
                continue
            tracks = sorted(languages[language], key=lambda item: {"json3": 0, "json": 1, "vtt": 2, "srt": 3}.get(item.get("ext"), 9))
            for track in tracks:
                extension = track.get("ext", "")
                if extension not in {"json3", "json", "vtt", "srt"}:
                    continue
                try:
                    content = track.get("data")
                    if content is None:
                        headers = {**(info.get("http_headers") or {}), **(track.get("http_headers") or {})}
                        with ydl.urlopen(Request(track["url"], headers=headers)) as response:
                            content = response.read(16 * 1024 * 1024 + 1)
                        if len(content) > 16 * 1024 * 1024:
                            continue
                    if isinstance(content, bytes):
                        content = content.decode("utf-8-sig")
                    rows = parse_subtitles(content, extension)
                    if rows:
                        return rows, {"language": language, "extraction_method": kind}
                except Exception:
                    continue
    return [], {}


def _stream(info: dict, *, video: bool = False) -> dict | None:
    formats = info.get("formats") or [info]
    candidates = [item for item in formats if item.get("url", "").startswith(("https://", "http://"))
                  and not item.get("has_drm") and item.get("vcodec" if video else "acodec") != "none"]
    if not video:
        candidates.sort(key=lambda item: (item.get("vcodec") != "none", item.get("height") or 0))
    else:
        candidates.sort(key=lambda item: abs((item.get("height") or 720) - 720))
    return candidates[0] if candidates else None


def _remote_audio(ydl, info: dict, directory: Path) -> Path:
    stream = _stream(info)
    if not stream:
        raise RuntimeError("该链接没有可读取的音轨；请提供字幕或本地视频")
    if stream.get("vcodec") == "none":
        # Select audio-only: never save a remote video as an intermediate download.
        from yt_dlp import YoutubeDL
        options = {**ydl.params, "format": stream["format_id"],
                   "outtmpl": str(directory / "remote-audio.%(ext)s")}
        with YoutubeDL(options) as downloader:
            downloaded = downloader.extract_info(info.get("webpage_url") or info["original_url"], download=True)
            for entry in downloaded.get("requested_downloads") or []:
                if entry.get("filepath") and Path(entry["filepath"]).is_file():
                    return Path(entry["filepath"])
            candidate = Path(downloader.prepare_filename(downloaded))
            if candidate.is_file():
                return candidate
        raise RuntimeError("音轨下载未产生文件；请提供本地音频")
    executable = _ffmpeg()
    if not executable:
        raise RuntimeError("该视频没有独立音轨，需要 ffmpeg；运行依赖安装后重试，或提供本地音频")
    target = directory / "remote-audio.wav"
    headers = {**(info.get("http_headers") or {}), **(stream.get("http_headers") or {})}
    command = [executable, "-hide_banner", "-loglevel", "error", "-y", *_input_args(stream["url"], headers),
               "-vn", "-ac", "1", "-ar", "16000", str(target)]
    operation = subprocess.run(command, capture_output=True, timeout=7200)
    if operation.returncode or not target.is_file() or target.stat().st_size <= 44:
        raise RuntimeError("远端音轨读取失败（可能需要登录）；请提供本地音频/视频或明确授权的 Cookie 文件")
    return target


def extract(source: str, work_dir: Path, *, cookies_file: str | None = None, model: str = "small",
            model_dir: Path | None = None) -> dict:
    remote = urlparse(source).scheme.lower() in {"http", "https"}
    path = None if remote else Path(source).expanduser().resolve()
    result = {"title": "视频内容" if remote else path.stem, "source": source,
              "source_type": "video_url" if remote else "audio" if path.suffix.lower() in AUDIO_EXTENSIONS else "video",
              "markdown": "", "assets": [], "status": "blocked", "warnings": [], "metadata": {}}
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="media-", dir=work_dir))
    rows, frames, info = [], [], {}
    visual_source, headers = None, None
    audio = path
    try:
        if remote:
            from yt_dlp import YoutubeDL
            options = {"quiet": True, "no_warnings": True, "logger": _QuietLogger(), "noplaylist": True,
                       "socket_timeout": 30, "retries": 1, "fragment_retries": 1, "cachedir": False}
            if cookies_file:
                cookie = Path(cookies_file).expanduser().resolve()
                if not cookie.is_file():
                    raise FileNotFoundError("指定的 Cookie 文件不存在")
                # yt-dlp may save its cookie jar on exit; preserve the supplied original.
                copied = directory / "session-cookies.txt"
                shutil.copyfile(cookie, copied)
                options["cookiefile"] = str(copied)
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(source, download=False)
                if not info or info.get("entries") is not None:
                    raise RuntimeError("请提供单个视频链接；当前链接返回了合集或空结果")
                if info.get("is_live"):
                    raise RuntimeError("正在直播的内容不能作为完整笔记归档；请提供回放")
                result["title"] = info.get("title") or result["title"]
                result["metadata"].update({key: info.get(key) for key in ("uploader", "duration", "webpage_url", "extractor_key") if info.get(key) is not None})
                rows, detail = _remote_subtitles(ydl, info)
                result["metadata"].update(detail)
                stream = _stream(info, video=True)
                if stream:
                    visual_source = stream["url"]
                    headers = {**(info.get("http_headers") or {}), **(stream.get("http_headers") or {})}
                if not rows and _stream(info):
                    audio = _remote_audio(ydl, info, directory)
                elif not rows:
                    result["warnings"].append("媒体没有可读取的音轨，需根据关键帧核对画面内容。")
        else:
            if not path.is_file():
                raise FileNotFoundError(f"媒体文件不存在：{path}")
            if path.suffix.lower() not in AUDIO_EXTENSIONS | VIDEO_EXTENSIONS:
                raise ValueError(f"不支持的媒体格式：{path.suffix}")
            result["assets"].append({"path": str(path), "name": path.name})
            if path.suffix.lower() in VIDEO_EXTENSIONS:
                visual_source = str(path)
            for extension in (".srt", ".vtt", ".json3", ".json"):
                subtitle = path.with_suffix(extension)
                if subtitle.is_file():
                    try:
                        rows = parse_subtitles(subtitle.read_text(encoding="utf-8-sig"), extension)
                    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError) as error:
                        result["warnings"].append(f"字幕解析失败：{_brief(error)}")
                    if rows:
                        result["assets"].append({"path": str(subtitle), "name": subtitle.name})
                        result["metadata"]["extraction_method"] = "sidecar_subtitles"
                        break
        if not rows and audio is not None:
            try:
                rows, detail = _transcribe(audio, model, model_dir=model_dir)
                result["metadata"].update(detail)
                if rows:
                    result["warnings"].append("正文来自本地自动转写；人名、术语和数字需结合原始媒体复核。")
            except Exception as error:
                result["warnings"].append(f"本地转写不可用：{_brief(error)}；请安装依赖并确认模型下载网络可用，可用 OBSIDIAN_INGEST_MODEL_DIR 指定模型缓存后重试，或提供同名 SRT/VTT 字幕。")
        if visual_source:
            try:
                frames = _keyframes(visual_source, directory, duration=info.get("duration"), headers=headers)
            except Exception as error:
                result["warnings"].append(f"关键帧提取失败：{_brief(error)}")
            if not frames:
                result["warnings"].append("没有提取到关键帧；如内容依赖画面，需提供截图或安装 imageio-ffmpeg 后重试。")
        result["status"] = "complete" if rows else "needs_transcript"
        result["metadata"]["visual_review_required"] = bool(visual_source)
        result["metadata"]["segments"] = len(rows)
        if rows:
            result["markdown"] = "## 原文字幕或转写\n\n" + "\n\n".join(f"[{timestamp(start)}] {value}" for start, value in rows)
        else:
            result["warnings"].append("未取得有效字幕或语音正文，不能仅凭标题和描述归档；请检查关键帧补充画面内容，或提供字幕/可读取的原文件。")
        if frames:
            result["markdown"] += "\n\n## 关键帧（待核对画面内容）\n\n" + "\n\n".join(f"![关键帧 {timestamp(start)}]({frame.name})" for start, frame in frames)
            result["assets"].extend({"path": str(frame), "name": frame.name} for _, frame in frames)
    except Exception as error:
        result["warnings"].append(f"媒体提取受阻：{_brief(error)}；请先更新或安装 yt-dlp，若需登录或出现平台验证，请提供本地文件/字幕或明确授权的 Cookie 文件。")
    finally:
        copied = directory / "session-cookies.txt"
        if copied.exists():
            copied.unlink()
    return result
