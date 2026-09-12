"""Extract sources without writing to the vault. Optional libraries load on demand."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from statistics import median
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
ATTACHMENT_EXTENSIONS = IMAGE_EXTENSIONS | {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".pptx", ".ppt",
    ".mp4", ".mov", ".webm", ".mkv", ".mp3", ".wav", ".m4a", ".ogg",
}
MAX_WEB_BYTES = 25 * 1024 * 1024
_OCR_ENGINE = None


def _result(source, source_type, title):
    return {"title": title, "source": source, "source_type": source_type,
            "markdown": "", "assets": [], "status": "complete", "warnings": [], "metadata": {}}


def _asset(data, suffix, work_dir, result, label="asset"):
    suffix = suffix.lower() if re.fullmatch(r"\.[a-zA-Z0-9]{1,8}", suffix) else ".bin"
    name = f"{label}-{hashlib.sha256(data).hexdigest()[:16]}{suffix}"
    target = work_dir / name
    if not target.exists():
        target.write_bytes(data)
    if not any(item["name"] == name for item in result["assets"]):
        result["assets"].append({"path": str(target.resolve()), "name": name})
    return name


def _keep_original(path, work_dir, result):
    name = _asset(path.read_bytes(), path.suffix, work_dir, result, "source")
    result["metadata"]["original_asset"] = name
    return name


def _decode(data):
    for encoding in ("utf-8-sig", "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeError:
            pass
    raise ValueError("无法可靠识别文本编码，请先转为 UTF-8。")


def _local_links(markdown, source_path, work_dir, result):
    source_dir = source_path.parent.resolve()
    def replace(match):
        raw = match.group(2).strip("<>")
        if urlparse(raw).scheme or raw.startswith(("#", "//")):
            return match.group(0)
        target = (source_dir / unquote(raw)).resolve()
        if not target.is_relative_to(source_dir):
            result["warnings"].append(f"附件引用越出源文档目录，未读取或复制，保留原引用：{raw}")
            return match.group(0)
        if target.suffix.lower() not in ATTACHMENT_EXTENSIONS:
            result["warnings"].append(f"链接不属于自动保存的媒体或附件类型，保留原引用：{raw}")
            return match.group(0)
        if not target.is_file():
            result["warnings"].append(f"附件不存在，保留原引用：{raw}")
            return match.group(0)
        name = _asset(target.read_bytes(), target.suffix, work_dir, result)
        return f"{match.group(1)}(<{name}>)"
    return re.sub(r"(!?\[[^\]\n]*\])\((<[^>\n]+>|[^\s)]+)\)", replace, markdown)


def _fetch(url):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ObsidianContentCollector/1.0)"}
    try:
        import requests
    except ImportError:
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            data = response.read(MAX_WEB_BYTES + 1)
            return data, response.geturl(), response.headers.get("Content-Type", "")
    with requests.get(url, headers=headers, timeout=(10, 30), stream=True) as response:
        response.raise_for_status()
        if urlparse(response.url).scheme not in {"http", "https"}:
            raise ValueError("不支持网页重定向协议。")
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_WEB_BYTES:
                raise ValueError("网页超过 25 MB，请提供下载后的本地文件。")
            chunks.append(chunk)
        return b"".join(chunks), response.url, response.headers.get("Content-Type", "")


def _html(data, source, work_dir, result, remote):
    try:
        from bs4 import BeautifulSoup
        from markdownify import markdownify
    except ImportError:
        from html.parser import HTMLParser
        class PlainText(HTMLParser):
            def __init__(self):
                super().__init__(); self.parts = []; self.skip = 0
            def handle_starttag(self, tag, attrs):
                if tag in {"script", "style"}: self.skip += 1
                if tag in {"p", "br", "div", "h1", "h2", "li"}: self.parts.append("\n")
            def handle_endtag(self, tag):
                if tag in {"script", "style"}: self.skip = max(0, self.skip - 1)
            def handle_data(self, data):
                if not self.skip: self.parts.append(data)
        parser = PlainText(); parser.feed(_decode(data))
        result["markdown"] = "".join(parser.parts).strip()
        result["status"] = "needs_browser" if remote else "blocked"
        result["warnings"].append("缺少 beautifulsoup4/markdownify，仅提取了纯文本，结构与图片待补齐。")
        return
    soup = BeautifulSoup(data, "html.parser")
    title = soup.select_one("#activity-name, h1") or soup.title
    if title and title.get_text(strip=True):
        result["title"] = title.get_text(" ", strip=True)
    for tag in soup.select("script, style, noscript, iframe, nav, footer, form"):
        tag.decompose()
    body = soup.select_one("#js_content") or soup.article or soup.main or soup.body or soup
    visible = body.get_text(" ", strip=True)
    gate = ("访问过于频繁", "请完成验证", "安全验证", "环境异常", "请在微信客户端打开", "captcha", "access denied")
    if remote and ((len(visible) < 1500 and any(token in visible.lower() for token in gate)) or
                   len(visible.replace(result["title"], "").strip()) < 80):
        result["status"] = "needs_browser"
        result["warnings"].append("网页可能只返回标题、验证页或短预览；需要通过已登录浏览器读取正文。")
    for image in body.find_all("img"):
        src = image.get("data-src") or image.get("data-original") or image.get("src", "")
        if src:
            image["src"] = urljoin(source, src) if remote else src
        if not image.get("alt"):
            image["alt"] = image.get("title", "原文图片")
    for link in body.find_all("a", href=True):
        if remote:
            link["href"] = urljoin(source, link["href"])
        if urlparse(link["href"]).scheme.lower() in {"javascript", "vbscript", "data"}:
            del link["href"]
    rendered = markdownify(str(body), heading_style="ATX", bullets="-", strip=["svg"])
    result["markdown"] = re.sub(r"\n{3,}", "\n\n", rendered).strip()
    if not remote:
        result["markdown"] = _local_links(result["markdown"], Path(source), work_dir, result)
    result["metadata"]["extraction_method"] = "html-main-content"


def _ocr(path, work_dir=None, result=None, page=None):
    global _OCR_ENGINE
    try:
        from rapidocr_onnxruntime import RapidOCR
        if _OCR_ENGINE is None:
            _OCR_ENGINE = RapidOCR(text_score=0.0)
        rows, _ = _OCR_ENGINE(str(path))
        if not rows:
            return "", "OCR 未识别到文字；需要视觉检查。"
        initial_rows = rows
        small_text = median(max(p[1] for p in row[0]) - min(p[1] for p in row[0]) for row in rows) < 24
        scale, retry_warning = 1.0, None
        if small_text:
            try:
                import cv2
                import numpy as np
                image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
                if image is None:
                    raise ValueError("无法读取放大所需的原图")
                attempt_scale = max(1.0, min(3.0, 3072 / max(image.shape[:2])))
                if attempt_scale > 1:
                    enlarged = cv2.resize(image, None, fx=attempt_scale, fy=attempt_scale, interpolation=cv2.INTER_CUBIC)
                    refined, _ = _OCR_ENGINE(enlarged)
                    if refined:
                        scale = attempt_scale
                        rows = [([[float(x) / scale, float(y) / scale] for x, y in quad], text, score)
                                for quad, text, score in refined]
                    else:
                        retry_warning = "小字号放大重识别未返回文字，保留初次结果。"
            except Exception as error:
                retry_warning = f"小字号重识别未完成，保留初次结果：{type(error).__name__}。"
        from ocr_layout import render_layout
        layout = render_layout(path, rows)
        pending = bool(layout["tables"] or layout["review_required"] or small_text)
        warnings = list(layout["warnings"])
        if small_text:
            warnings.append(f"检测到小字号文字，放大重识别比例 {scale:g}；数字仍需对照原图核验，不能只依据 OCR 置信度。")
        if retry_warning:
            warnings.append(retry_warning)
        if pending:
            warnings.append("图片 OCR 文字或布局需要对照原图核验；若含表格，不能将散文作为完整表格入库。")
        if result is not None and work_dir is not None:
            evidence = Path(work_dir) / (Path(path).stem + "-ocr.json")
            evidence.write_text(json.dumps({"image": str(Path(path).resolve()), "ocr_scale": scale,
                "rows": [{"box": [[float(x), float(y)] for x, y in row[0]],
                          "text": str(row[1]), "confidence": float(row[2])} for row in rows],
                "initial_rows": [{"box": [[float(x), float(y)] for x, y in row[0]],
                                  "text": str(row[1]), "confidence": float(row[2])} for row in initial_rows],
                "layout": layout}, ensure_ascii=False, indent=2), encoding="utf-8")
            metadata = result["metadata"]
            metadata.setdefault("ocr_layout", []).append({"image": str(Path(path).resolve()),
                "evidence_file": str(evidence.resolve()), "page": page, "tables": layout["tables"],
                "review_required": pending, "ocr_scale": scale})
            metadata["layout_review_required"] = metadata.get("layout_review_required", False) or pending
        return layout["markdown"], "；".join(warnings) if warnings else None
    except ImportError:
        return "", "未安装 rapidocr_onnxruntime，需要安装 OCR 依赖或由宿主读取图片。"
    except Exception as error:
        return "", f"OCR 未完成：{type(error).__name__}: {error}"


def _image(path, work_dir, result):
    from PIL import Image
    original = _keep_original(path, work_dir, result)
    with Image.open(path) as image:
        result["metadata"].update({"width": image.width, "height": image.height})
        frame_count = getattr(image, "n_frames", 1)
    text, warning = _ocr(path, work_dir, result)
    result["markdown"] = f"![原始图片](<{original}>)\n\n{text}".strip()
    if warning or frame_count > 1:
        result["status"] = "needs_ocr"
        result["warnings"].append(warning or "多帧图片仅 OCR 首帧，剩余帧需要视觉检查。")
    result["metadata"].update({"extraction_method": "ocr", "frame_count": frame_count})


def _docx(path, work_dir, result):
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    document = Document(path)
    original = _keep_original(path, work_dir, result)

    def inline(element):
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "t": return element.text or ""
        if tag == "tab": return "\t"
        if tag in {"br", "cr"}: return "\n"
        if tag == "blip":
            rid = element.get(qn("r:embed"))
            if rid and rid in document.part.related_parts:
                part = document.part.related_parts[rid]
                name = _asset(part.blob, Path(str(part.partname)).suffix, work_dir, result)
                return f"\n\n![文档图片](<{name}>)\n\n"
            return ""
        content = "".join(inline(child) for child in element)
        if tag == "hyperlink":
            relation = document.part.rels.get(element.get(qn("r:id")))
            if relation and relation.is_external:
                return f"[{content}](<{relation.target_ref}>)"
        return content

    def block(element, parent):
        if element.tag == qn("w:p"):
            paragraph = Paragraph(element, parent)
            content = inline(element).strip()
            style = paragraph.style.name if paragraph.style else ""
            heading = re.search(r"(?:Heading|标题)\s*(\d)", style, re.I)
            if heading: content = "#" * min(int(heading[1]), 6) + " " + content
            elif "List" in style or element.find("w:pPr/w:numPr", element.nsmap) is not None:
                content = "- " + content
            return content
        if element.tag == qn("w:tbl"):
            table = Table(element, parent)
            rows = [["<br>".join(block(child, cell) for child in cell._tc
                                if child.tag in {qn("w:p"), qn("w:tbl")}).strip().replace("|", "\\|").replace("\n", "<br>")
                     for cell in row.cells] for row in table.rows]
            if not rows: return ""
            width = max(map(len, rows))
            lines = ["| " + " | ".join(row + [""] * (width - len(row))) + " |" for row in rows]
            lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
            return "\n".join(lines)
        return ""

    pieces = [block(element, document) for element in document.element.body]
    result["markdown"] = "\n\n".join(piece for piece in pieces if piece).strip()
    if not result["markdown"]:
        result["status"] = "blocked"
        result["warnings"].append("DOCX 未提取到正文，请检查文档或提供导出的 PDF。")
    result["markdown"] += f"\n\n[原始 Word 文件](<{original}>)"
    result["metadata"]["extraction_method"] = "docx-body-xml"
    result["warnings"].append("已保留正文、表格及嵌入图片；页眉页脚、批注和修订显示以原始 Word 为准。")


def _pdf(path, work_dir, result):
    original = _keep_original(path, work_dir, result)
    try:
        import pymupdf as fitz
    except ImportError:
        fitz = None
    pages, pieces = [], []
    if fitz is None:
        from pypdf import PdfReader
        document = PdfReader(path)
        if document.is_encrypted and not document.decrypt(""):
            raise ValueError("PDF 已加密，需要提供解密后的文件。")
        for index, page in enumerate(document.pages, 1):
            content = (page.extract_text() or "").strip()
            page_assets = []
            for item in page.images:
                name = _asset(item.data, Path(item.name).suffix, work_dir, result)
                page_assets.append(f"![第 {index} 页图片](<{name}>)")
            pending = len(content) < 40 or bool(page_assets)
            pages.append({"page": index, "status": "needs_ocr" if pending else "complete", "method": "pypdf"})
            pieces.append(f"## 第 {index} 页\n\n{content}\n\n" + "\n\n".join(page_assets))
        result["warnings"].append("未安装 PyMuPDF；使用文本提取，含图片或少量文字的页面需要进一步检查。")
    else:
        with fitz.open(path) as document:
            if document.needs_pass:
                raise ValueError("PDF 已加密，需要提供解密后的文件。")
            for index, page in enumerate(document, 1):
                content = page.get_text("text", sort=True).strip()
                images = page.get_images(full=True)
                large_image = any(rect.get_area() > page.rect.get_area() * 0.55
                                  for item in images for rect in page.get_image_rects(item[0]))
                pending = len(content) < 40 or large_image
                visual = ""
                if pending or images or page.get_drawings():
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(1.7, 1.7), alpha=False)
                    name = _asset(pixmap.tobytes("png"), ".png", work_dir, result, f"page-{index}")
                    visual = f"\n\n![第 {index} 页原貌](<{name}>)"
                    if pending:
                        ocr_text, warning = _ocr(work_dir / name, work_dir, result, page=index)
                        if ocr_text and not warning:
                            content, pending = ocr_text, False
                        elif ocr_text:
                            content += "\n\nOCR 补充：\n" + ocr_text
                        if warning: result["warnings"].append(f"第 {index} 页：{warning}")
                pages.append({"page": index, "status": "needs_ocr" if pending else "complete",
                              "method": "ocr" if large_image or len(page.get_text().strip()) < 40 else "text"})
                pieces.append(f"## 第 {index} 页\n\n{content}{visual}")
    result["metadata"].update({"pages": pages, "page_count": len(pages), "extraction_method": "pdf-pages"})
    if not pages:
        result["status"] = "blocked"; result["warnings"].append("PDF 没有可读取页面。")
    elif any(page["status"] != "complete" for page in pages):
        result["status"] = "needs_ocr"
    result["markdown"] = "\n\n".join(pieces) + f"\n\n[原始 PDF](<{original}>)"
    result["warnings"].append("PDF 文本按页面提取；复杂版式、表格和公式请结合保留的原文核对。")


def extract(source: str, work_dir: Path) -> dict:
    """Return extracted Markdown and staged assets; incomplete sources stay explicit."""
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    remote = urlparse(source).scheme.lower() in {"http", "https"}
    path = None if remote else Path(source).expanduser().resolve()
    kind = "web" if remote else (path.suffix.lower().lstrip(".") or "unknown")
    result = _result(source, kind, unquote(Path(urlparse(source).path).stem) if remote else path.stem)
    try:
        if remote:
            data, final_url, content_type = _fetch(source)
            if len(data) > MAX_WEB_BYTES: raise ValueError("网页超过 25 MB，请提供本地文件。")
            result["metadata"]["resolved_url"] = final_url
            if "application/pdf" in content_type or data.startswith(b"%PDF-"):
                name = _asset(data, ".pdf", work_dir, result, "source")
                result["source_type"] = "pdf"; _pdf(work_dir / name, work_dir, result)
            elif content_type and not any(part in content_type for part in ("html", "text/")):
                result["status"] = "blocked"
                result["warnings"].append(f"链接返回 {content_type}，请下载后作为本地文件提供。")
            else:
                _html(data, final_url, work_dir, result, True)
        elif not path.is_file():
            raise FileNotFoundError(f"文件不存在：{path}")
        elif kind in {"md", "markdown", "txt"}:
            content = _decode(path.read_bytes())
            if "\x00" in content: raise ValueError("检测到二进制内容，不能当作文本导入。")
            result["markdown"] = _local_links(content, path, work_dir, result)
            heading = re.search(r"^#\s+(.+)$", content, re.M)
            if heading: result["title"] = heading[1].strip()
            if not content.strip(): result["status"] = "blocked"; result["warnings"].append("文件为空。")
        elif kind in {"html", "htm"}: _html(path.read_bytes(), str(path), work_dir, result, False)
        elif kind == "docx": _docx(path, work_dir, result)
        elif kind == "pdf": _pdf(path, work_dir, result)
        elif path.suffix.lower() in IMAGE_EXTENSIONS: _image(path, work_dir, result)
        else:
            result["status"] = "blocked"
            result["warnings"].append("旧版 .doc 请先转为 .docx 或 PDF。" if kind == "doc" else f"不支持 .{kind} 文件；请转换为受支持格式。")
    except Exception as error:
        result["status"] = "needs_browser" if remote and kind == "web" else "blocked"
        result["warnings"].append(f"提取失败：{type(error).__name__}: {error}")
    return result
