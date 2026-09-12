"""Content ingestion for Codex/OpenClaw. Uses vault files, never Obsidian IPC."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SKILL = Path(__file__).resolve().parents[1]
if (SKILL / ".deps").is_dir():
    sys.path.insert(0, str(SKILL / ".deps"))

MEDIA = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".wmv", ".ts", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".wma"}
MEDIA_HOSTS = ("douyin.com", "bilibili.com", "b23.tv", "youtube.com", "youtu.be", "tiktok.com")
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)
INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
TRACKING = {"fbclid", "gclid"}
CONFIG_DEFAULTS = {"vault_path": "", "attachment_folder": "_attachments/收录",
                   "work_dir": ".work", "model_dir": ".models", "whisper_model": "small"}


def json_write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ingest-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def json_read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest_file(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_source(source: str):
    source = source.strip()
    match = re.search(r"https?://[^\s<>\"“”]+", source)
    if match:
        source = match.group(0).rstrip("。，、；！）)]")
    return source


def canonical_url(source: str):
    parts = urlsplit(source)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Only HTTP(S) source URLs are supported")
    if parts.username or parts.password:
        raise ValueError("Do not put credentials in source URLs")
    host = parts.netloc.lower()
    dropped = set(TRACKING)
    if parts.hostname == "mp.weixin.qq.com":
        dropped.update({"from", "scene", "ascene", "clicktime", "enterid", "wx_header"})
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in dropped]
    if parts.hostname in {"www.bilibili.com", "bilibili.com"} and re.match(r"/video/(BV|av)", parts.path):
        query = [(k, v) for k, v in query if k == "p"]
    return urlunsplit((parts.scheme.lower(), host, parts.path, urlencode(sorted(query)), ""))


def source_identity(source: str):
    if source.startswith(("http://", "https://")):
        canonical = canonical_url(source)
        return canonical, hashlib.sha256(canonical.encode()).hexdigest()
    path = Path(source).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("The source must be a file, not a directory")
    checksum = digest_file(path)
    return str(path), hashlib.sha256(("file-sha256:" + checksum).encode()).hexdigest()


def safe_name(value: str, limit=100):
    value = INVALID.sub("_", value).strip().strip(".")
    value = re.sub(r"\s+", " ", value)[:limit].rstrip(" .") or "未命名"
    return "_" + value if RESERVED.match(value) else value


def relative_path(value: str):
    value = value.replace("\\", "/")
    if value.startswith("/"):
        raise ValueError("An absolute path is not a category")
    parts = value.split("/")
    if not parts or any(not p or p in {".", ".."} or p.startswith(".") or INVALID.search(p)
                        or p != p.rstrip(" .") or RESERVED.match(p) for p in parts):
        raise ValueError("Category/attachment path contains an unsafe component")
    return Path(*parts)


def confined(root: Path, relative: Path):
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()) or target == root.resolve():
        raise ValueError("Destination escapes the configured vault")
    return target


def config_file(path=None):
    selected = path or os.environ.get("OBSIDIAN_INGEST_CONFIG") or SKILL / "config.json"
    return Path(os.path.expandvars(str(selected))).expanduser().resolve()


def config_resolve(values, path: Path, require_vault=True):
    if not isinstance(values, dict):
        raise ValueError("Config must be a JSON object")
    config = {**CONFIG_DEFAULTS, **values}
    for key in CONFIG_DEFAULTS:
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"Config {key} must be a nonempty string")
    config["attachment_folder"] = relative_path(config["attachment_folder"]).as_posix()
    config["config_path"] = path
    for key, output in (("vault_path", "root"), ("work_dir", "work_root"), ("model_dir", "model_root")):
        value = config[key]
        if key == "model_dir":
            value = os.environ.get("OBSIDIAN_INGEST_MODEL_DIR") or value
        target = Path(os.path.expandvars(value)).expanduser()
        config[output] = (path.parent / target).resolve()
    if require_vault and not config["root"].is_dir():
        raise ValueError(f"Vault directory is unavailable: {config['root']}")
    confined(config["root"], relative_path(config["attachment_folder"]))
    return config


def config_load(path=None, require_vault=True):
    path = config_file(path)
    return config_resolve(json_read(path), path, require_vault)


def configure(args):
    path = config_file(args.config)
    if args.config_action == "set":
        updates = {key: getattr(args, key) for key in CONFIG_DEFAULTS if getattr(args, key) is not None}
        if not updates:
            raise ValueError("Provide at least one config setting to change")
        values = json_read(path) if path.exists() else {}
        if not isinstance(values, dict):
            raise ValueError("Config must be a JSON object")
        values.update(updates)
        config = config_resolve(values, path, require_vault=False)
        values = {**values, **{key: config[key] for key in CONFIG_DEFAULTS}}
        json_write(path, values)
    else:
        config = config_load(path, require_vault=False)
    return {"status": "complete", "config_file": str(path),
            "settings": {key: config[key] for key in CONFIG_DEFAULTS},
            "resolved_paths": {"vault_path": str(config["root"]),
                               "attachment_folder": str(config["root"] / config["attachment_folder"]),
                               "work_dir": str(config["work_root"]), "model_dir": str(config["model_root"])},
            "vault_available": config["root"].is_dir()}


def taxonomy(config):
    root = config["root"]
    ignored = {relative_path(config["attachment_folder"]).parts[0], "_attachments", "node_modules"}
    caches = {config.get("work_root"), config.get("model_root")}
    categories, total = [], 0
    for current, folders, filenames in os.walk(root, followlinks=False):
        folders[:] = sorted(n for n in folders if not n.startswith(".") and n not in ignored
                            and not (Path(current) / n).is_symlink()
                            and (Path(current) / n).resolve() not in caches)
        notes = sorted(n for n in filenames if n.lower().endswith(".md"))
        total += len(notes)
        category = Path(current).relative_to(root).as_posix()
        if category != "." and not category.startswith("pdf解析/") and category != "pdf解析":
            categories.append({"path": category, "direct_notes": len(notes),
                               "examples": [Path(n).stem for n in notes[:6]]})
    return {"vault_path": str(root), "markdown_count": total, "categories": categories}


def prepare(source: str, work_dir: Path | None, config, cookies_file=None):
    source = normalize_source(source)
    canonical, source_id = source_identity(source)
    if work_dir is None:
        config["work_root"].mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix="ingest-", dir=config["work_root"]))
    work_dir = work_dir.expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    # One source per working folder prevents mixing assets from concurrent jobs.
    bundle_path = work_dir / "bundle.json"
    if bundle_path.exists():
        previous = json_read(bundle_path)
        if previous.get("input_source") != source and previous.get("source_id") != source_id:
            raise ValueError("Use a separate work directory for each source")
    host = urlsplit(source).hostname or ""
    is_media = Path(urlsplit(source).path).suffix.lower() in MEDIA or any(
        host == name or host.endswith("." + name) for name in MEDIA_HOSTS)
    if is_media:
        from extract_media import extract
        result = extract(source, work_dir, cookies_file=cookies_file, model=config["whisper_model"],
                         model_dir=config.get("model_root"))
    else:
        from extract_sources import extract
        result = extract(source, work_dir)
    result.setdefault("warnings", [])
    result.setdefault("metadata", {})
    result.setdefault("assets", [])
    result.setdefault("title", Path(source).stem)
    result.setdefault("status", "blocked")
    body = result.pop("markdown", "")
    if result["status"] == "complete" and not body.strip():
        result["status"] = "blocked"
        result["warnings"].append("Extraction returned no text")
    final_source = (result["metadata"].get("resolved_url") or result["metadata"].get("webpage_url")
                    or result.get("source", source))
    if source.startswith(("http://", "https://")) and final_source.startswith(("http://", "https://")):
        canonical, source_id = source_identity(final_source)
    if not source.startswith(("http://", "https://")) and not result["metadata"].get("original_asset"):
        original = Path(source).expanduser().resolve()
        if not any(Path(asset["path"]).resolve() == original for asset in result["assets"]):
            result["assets"].append({"path": str(original), "name": original.name})
    (work_dir / "extracted.md").write_text(body, encoding="utf-8")
    result.update({"input_source": source, "source": canonical, "source_id": source_id,
                   "markdown_file": str(work_dir / "extracted.md"), "prepared_at": datetime.now(timezone.utc).isoformat()})
    json_write(bundle_path, result)
    return {"status": result["status"], "bundle": str(bundle_path), "markdown": result["markdown_file"],
            "title": result["title"], "warnings": result["warnings"], "assets": result["assets"]}


def complete(bundle_path, markdown_path, method):
    bundle = json_read(bundle_path)
    metadata = bundle.setdefault("metadata", {})
    if metadata.get("layout_review_required") and method != "vision":
        raise ValueError("Table layout requires verification against the source image; use --method vision after review")
    text = Path(markdown_path).read_text(encoding="utf-8-sig")
    if not text.strip():
        raise ValueError("A verified nonempty source transcription is required")
    target = Path(bundle["markdown_file"])
    target.write_text(text, encoding="utf-8")
    bundle.update({"status": "complete", "completion_method": method})
    metadata["completed_from_real_source"] = True
    if metadata.get("layout_review_required"):
        metadata["layout_review_required"] = False
        metadata["layout_verified"] = True
        for item in metadata.get("ocr_layout", []):
            item["review_required"] = False
        bundle.setdefault("warnings", []).append("上述 OCR/版式提示来自初次提取；本次正文已通过原图视觉核验补齐。")
    json_write(Path(bundle_path), bundle)
    return {"status": "complete", "bundle": str(bundle_path), "method": method}


def render(bundle, decision, body, asset_links):
    for original, relative in sorted(asset_links.items(), key=lambda item: len(item[0]), reverse=True):
        body = body.replace("(<" + original + ">)", "(<" + relative + ">)")
        body = body.replace("(" + original + ")", "(<" + relative + ">)")
    metadata = {"title": decision["title"], "source": bundle["source"], "source_type": bundle.get("source_type", "unknown"),
                "source_id": bundle["source_id"], "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "imported_at": datetime.now(timezone.utc).isoformat(), "category": decision["category"],
                "tags": decision.get("tags", []), "extraction_status": bundle["status"]}
    # Fingerprint original text before destination-relative attachment rewriting.
    metadata["content_sha256"] = hashlib.sha256(bundle["_original_body"].strip().encode()).hexdigest()
    for key in ("author", "published", "duration", "language", "layout_verified"):
        if bundle.get("metadata", {}).get(key) is not None:
            metadata[key] = bundle["metadata"][key]
    lines = ["---"] + [f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in metadata.items()] + ["---", "", "# " + decision["title"], ""]
    if decision.get("summary"):
        lines.extend(["## 摘要", "", decision["summary"].strip(), ""])
    lines.extend(["## 来源内容", "", body.strip(), "", "## 来源与附件", ""])
    source = bundle["source"]
    lines.append("- 来源：" + ("[原始链接](<" + source + ">)" if source.startswith(("http://", "https://")) else "`" + source + "`"))
    for name, link in asset_links.items():
        lines.append(f"- [{name.replace('[', '').replace(']', '')}](<{link}>)")
    if decision.get("classification_reason"):
        lines.append("- 归类依据：" + decision["classification_reason"].replace("\n", " "))
    if bundle.get("warnings"):
        lines.extend(["", "## 提取说明", ""] + ["- " + str(w) for w in bundle["warnings"]])
    return "\n".join(lines) + "\n", metadata


def import_note(config, bundle_path, decision_path, dry_run=False):
    root = config["root"]
    bundle, decision = json_read(bundle_path), json_read(decision_path)
    if bundle.get("status") != "complete":
        raise ValueError("Source extraction is incomplete; resolve it before importing")
    if bundle.get("metadata", {}).get("layout_review_required"):
        raise ValueError("Table layout is still awaiting verification against the source image")
    body = Path(bundle["markdown_file"]).read_text(encoding="utf-8-sig")
    if not body.strip():
        raise ValueError("Empty content cannot be imported")
    if not isinstance(decision.get("title"), str) or not decision["title"].strip():
        raise ValueError("A title is required")
    if not isinstance(decision.get("tags", []), list) or any(not isinstance(t, str) for t in decision.get("tags", [])):
        raise ValueError("tags must be a list of strings")
    decision["title"] = re.sub(r"[\r\n]+", " ", decision["title"]).strip()
    category = relative_path(decision["category"])
    if category.parts[0] in {"_attachments", relative_path(config["attachment_folder"]).parts[0]}:
        raise ValueError("Notes must go in a content category, not in attachments")
    decision["category"] = category.as_posix()
    destination = confined(root, category)
    attachment_root = confined(root, relative_path(config["attachment_folder"]))
    if destination == attachment_root or destination.is_relative_to(attachment_root):
        raise ValueError("Notes must go in a content category, not in attachments")
    source_id = bundle["source_id"]
    if not re.fullmatch("[a-f0-9]{64}", source_id):
        raise ValueError("Invalid source fingerprint")
    content_hash = hashlib.sha256(body.strip().encode()).hexdigest()
    state = confined(root, Path("_attachments") / ".obsidian-ingest")
    # This index only tracks content ingested by this skill; old notes are untouched.
    index_path = state / "index.json"
    index = json_read(index_path) if index_path.exists() else {"records": []}
    for record in index["records"]:
        if source_id == record["source_id"] or content_hash == record["content_sha256"]:
            existing = confined(root, relative_path(record["path"]))
            if existing.is_file():
                return {"status": "duplicate", "path": str(existing), "reason": "source_or_content_already_imported"}
    stem = safe_name(decision["title"])
    note = destination / (stem + ".md")
    if note.exists():
        note = destination / (stem + " (" + source_id[:8] + ").md")
    if note.exists():
        raise ValueError("Both target filenames exist; no original will be overwritten")
    confined(root, note.relative_to(root))
    asset_dir = confined(root, relative_path(config["attachment_folder"]) / source_id[:16])
    assets, links, names = [], {}, set()
    for asset in bundle.get("assets", []):
        original = Path(asset["path"]).resolve(strict=True)
        if not original.is_file():
            raise ValueError("An attachment is not a file")
        requested = asset.get("name", original.name)
        suffix = Path(requested).suffix
        name = safe_name(Path(requested).stem, 85) + INVALID.sub("_", suffix)[:12] if suffix else safe_name(requested)
        if name in names:
            raise ValueError("Attachment names must be unique")
        names.add(name)
        target = confined(root, asset_dir.relative_to(root) / name)
        links[requested] = Path(os.path.relpath(target, destination)).as_posix()
        assets.append((original, target))
    bundle["_original_body"] = body
    markdown, _ = render(bundle, decision, body, links)
    if dry_run:
        return {"status": "planned", "path": str(note), "category_is_new": not destination.exists(),
                "attachments": [str(target) for _, target in assets], "characters": len(markdown)}
    state.mkdir(parents=True, exist_ok=True)
    lock = state / "import.lock"
    # A concurrent ingest must retry after the active job; never steal a live lock.
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError(f"Another import is active; retry later. If a previous process crashed, verify before removing {lock}")
    try:
        os.write(lock_fd, str(os.getpid()).encode())
        os.close(lock_fd)
        # Recheck under the lock, including a request that planned concurrently.
        current = json_read(index_path) if index_path.exists() else {"records": []}
        for record in current["records"]:
            if source_id == record["source_id"] or content_hash == record["content_sha256"]:
                existing = confined(root, relative_path(record["path"]))
                if existing.is_file():
                    return {"status": "duplicate", "path": str(existing), "reason": "source_or_content_already_imported"}
        destination.mkdir(parents=True, exist_ok=True)
        for original, target in assets:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if digest_file(target) != digest_file(original):
                    raise ValueError("An attachment collision would overwrite different content")
            else:
                # Exclusive creation protects against both concurrent and preexisting files.
                with original.open("rb") as incoming, target.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                if digest_file(target) != digest_file(original):
                    raise ValueError("Attachment verification failed")
        created = False
        try:
            with note.open("x", encoding="utf-8", newline="\n") as outgoing:
                created = True
                outgoing.write(markdown)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        except OSError:
            if created:
                note.unlink(missing_ok=True)
            raise
        if note.read_text(encoding="utf-8") != markdown:
            raise ValueError("Markdown read-back verification failed")
        current["records"].append({"source_id": source_id, "content_sha256": content_hash,
                                   "path": note.relative_to(root).as_posix()})
        json_write(index_path, current)
        return {"status": "imported", "path": str(note), "category": decision["category"],
                "attachments": len(assets), "verified": True}
    finally:
        lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    settings = commands.add_parser("config")
    actions = settings.add_subparsers(dest="config_action", required=True)
    actions.add_parser("show")
    edit = actions.add_parser("set")
    for key in CONFIG_DEFAULTS:
        edit.add_argument("--" + key.replace("_", "-"))
    commands.add_parser("doctor")
    tax = commands.add_parser("taxonomy")
    tax.add_argument("--out", type=Path)
    prep = commands.add_parser("prepare")
    prep.add_argument("source")
    prep.add_argument("--work-dir", type=Path)
    prep.add_argument("--cookies-file")
    finish = commands.add_parser("complete")
    finish.add_argument("--bundle", type=Path, required=True)
    finish.add_argument("--markdown", type=Path, required=True)
    finish.add_argument("--method", choices=["browser", "vision", "manual-transcript"], required=True)
    imp = commands.add_parser("import")
    imp.add_argument("--bundle", type=Path, required=True)
    imp.add_argument("--decision", type=Path, required=True)
    imp.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        config = config_load(args.config) if args.command != "config" else None
        if args.command == "config":
            result = configure(args)
        elif args.command == "doctor":
            modules = {"bs4": "BeautifulSoup", "markdownify": "markdownify", "requests": "get", "pymupdf": "open", "cv2": "morphologyEx",
                       "docx": "Document", "PIL.Image": "open", "rapidocr_onnxruntime": "RapidOCR",
                       "yt_dlp": "YoutubeDL", "faster_whisper": "WhisperModel", "imageio_ffmpeg": "get_ffmpeg_exe"}
            dependencies, errors = {}, {}
            for name, attribute in modules.items():
                try:
                    getattr(importlib.import_module(name), attribute)
                    dependencies[name] = True
                except (ImportError, AttributeError, OSError) as error:
                    dependencies[name] = False
                    errors[name] = str(error)
            result = {"status": "ready", "vault_path": str(config["root"]), "obsidian_required": False,
                      "python": sys.executable, "writable": os.access(config["root"], os.W_OK),
                      "dependencies": dependencies, "dependency_errors": errors}
            if not result["writable"] or not all(result["dependencies"].values()):
                result["status"] = "needs_setup"
        elif args.command == "taxonomy":
            result = taxonomy(config)
            if args.out:
                json_write(args.out, result)
                result = {"status": "complete", "taxonomy": str(args.out), "categories": len(result["categories"]), "markdown_count": result["markdown_count"]}
        elif args.command == "prepare":
            result = prepare(args.source, args.work_dir, config, args.cookies_file)
        elif args.command == "complete":
            result = complete(args.bundle, args.markdown, args.method)
        else:
            result = import_note(config, args.bundle, args.decision, args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {None, "ready", "complete", "imported", "duplicate", "planned"} else 2
    except (OSError, ValueError, KeyError, ImportError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
