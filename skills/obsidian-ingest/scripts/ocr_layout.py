"""Conservative ruled-table drafts from RapidOCR boxes; no model downloads.

Only axis-aligned, closed rectangular grids are reconstructed. Uncertain grids
and possible borderless tables keep their OCR text and request visual review.
"""
from __future__ import annotations

from html import escape


def _lines(words):
    """Join OCR fragments on a shared baseline without discarding empty cells."""
    lines = []
    for word in sorted(words, key=lambda w: (w["cy"], w["x0"])):
        if lines and abs(word["cy"] - lines[-1][0]["cy"]) <= min(word["height"], lines[-1][0]["height"]) * .45:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [" ".join(w["text"] for w in sorted(line, key=lambda w: w["x0"])) for line in lines]


def _axes(projection, minimum):
    groups = []
    for index, count in enumerate(projection):
        if count < minimum:
            continue
        if groups and index - groups[-1][-1] <= 3:
            groups[-1].append(index)
        else:
            groups.append([index])
    return [round(sum(group) / len(group)) for group in groups]


def _longest_runs(mask):
    """Aligned glyph stems are not one continuous table border."""
    import numpy as np
    run = np.zeros(mask.shape[1], dtype=np.int32)
    longest = run.copy()
    for row in mask:
        run = (run + 1) * (row != 0)
        longest = np.maximum(longest, run)
    return longest


def _grid(horizontal, vertical, xs, ys):
    """Validate every edge, then merge cells only across clearly absent edges."""
    import numpy as np
    nr, nc = len(ys) - 1, len(xs) - 1

    def edge(mask, fixed, start, end, horizontal_edge):
        inset = min(4, max(1, (end - start) // 5))
        stripe = (mask[max(0, fixed - 2):fixed + 3, start + inset:end - inset]
                  if horizontal_edge else mask[start + inset:end - inset, max(0, fixed - 2):fixed + 3])
        if not stripe.size:
            raise ValueError("单元格过小")
        coverage = np.any(stripe, axis=0 if horizontal_edge else 1).mean()
        if coverage > .72:
            return True
        if coverage < .16:
            return False
        raise ValueError("边框不完整或模糊")

    hedges = [[edge(horizontal, y, xs[c], xs[c + 1], True) for c in range(nc)] for y in ys]
    vedges = [[edge(vertical, x, ys[r], ys[r + 1], False) for x in xs] for r in range(nr)]
    if not (all(hedges[0]) and all(hedges[-1]) and all(row[0] and row[-1] for row in vedges)):
        raise ValueError("表格外框不闭合")
    pending, cells = {(r, c) for r in range(nr) for c in range(nc)}, []
    while pending:
        seed = min(pending)
        pending.remove(seed)
        group, queue = {seed}, [seed]
        while queue:
            r, c = queue.pop()
            neighbors = []
            if r > 0 and not hedges[r][c]: neighbors.append((r - 1, c))
            if r + 1 < nr and not hedges[r + 1][c]: neighbors.append((r + 1, c))
            if c > 0 and not vedges[r][c]: neighbors.append((r, c - 1))
            if c + 1 < nc and not vedges[r][c + 1]: neighbors.append((r, c + 1))
            for neighbor in neighbors:
                if neighbor in pending:
                    pending.remove(neighbor); group.add(neighbor); queue.append(neighbor)
        r0, r1 = min(r for r, c in group), max(r for r, c in group) + 1
        c0, c1 = min(c for r, c in group), max(c for r, c in group) + 1
        if len(group) != (r1 - r0) * (c1 - c0):
            raise ValueError("非矩形合并单元格")
        if (any(hedges[r][c] for r in range(r0 + 1, r1) for c in range(c0, c1)) or
                any(vedges[r][c] for r in range(r0, r1) for c in range(c0 + 1, c1))):
            raise ValueError("合并单元格内仍有分隔线")
        cells.append({"r0": r0, "r1": r1, "c0": c0, "c1": c1, "words": []})
    return sorted(cells, key=lambda cell: (cell["r0"], cell["c0"]))


def _render(cells, nr, nc):
    merged = any(cell["r1"] - cell["r0"] > 1 or cell["c1"] - cell["c0"] > 1 for cell in cells)
    if merged:
        output = ["<table>"]
        for r in range(nr):
            output.append("<tr>")
            for cell in cells:
                if cell["r0"] != r:
                    continue
                span = f' rowspan="{cell["r1"] - r}" colspan="{cell["c1"] - cell["c0"]}"'
                body = "<br>".join(escape(line) for line in _lines(cell["words"]))
                output.append(f"<td{span}>{body}</td>")
            output.append("</tr>")
        return "\n".join(output + ["</table>"])
    matrix = [[""] * nc for _ in range(nr)]
    for cell in cells:
        matrix[cell["r0"]][cell["c0"]] = "<br>".join(
            escape(line, quote=False).replace("\\", "\\\\").replace("|", "\\|") for line in _lines(cell["words"]))
    output = ["| " + " | ".join(row) + " |" for row in matrix]
    output.insert(1, "| " + " | ".join(["---"] * nc) + " |")
    return "\n".join(output)


def _possible_columns(words):
    """Review repeated separated columns, never synthesize their cell boundaries."""
    lines = []
    for word in sorted(words, key=lambda w: (w["cy"], w["x0"])):
        if lines and abs(word["cy"] - lines[-1][0]["cy"]) < word["height"] * .5:
            lines[-1].append(word)
        else:
            lines.append([word])
    patterns = []
    for line in lines:
        line.sort(key=lambda w: w["x0"])
        if len(line) < 2 or any(b["x0"] - a["x1"] < max(a["height"], b["height"]) for a, b in zip(line, line[1:])):
            continue
        signature = [w["x0"] for w in line]
        tolerance = max(6, min(w["height"] for w in line))
        for pattern in patterns:
            shorter, longer = sorted((signature, pattern[0]), key=len)
            if all(any(abs(a - b) <= tolerance for b in longer) for a in shorter):
                pattern[0] = longer
                pattern[1] += 1
                if pattern[1] >= 3:
                    return True
                break
        else:
            patterns.append([signature, 1])
    return False


def render_layout(path, rows):
    """Return markdown, tables (count), review_required (bool), warnings (list).

    rows is RapidOCR's sequence of (four-point quad, text, confidence). Text with
    confidence below .65 is retained with a marker. Successful tables are drafts:
    the caller may enforce visual verification before accepting them.
    """
    words, warnings = [], []
    for quad, text, score in rows:
        xs, ys = [float(p[0]) for p in quad], [float(p[1]) for p in quad]
        uncertain = score is None or float(score) < .65
        words.append({"x0": min(xs), "x1": max(xs), "y0": min(ys), "y1": max(ys),
                      "cx": sum(xs) / len(xs), "cy": sum(ys) / len(ys), "height": max(1, max(ys) - min(ys)),
                      "text": ("[待核验]" if uncertain else "") + str(text)})
        if uncertain and not warnings:
            warnings.append("存在低置信度 OCR 文字，已保留并标记 [待核验]，需对照原图复核。")
    result = {"markdown": "\n".join(_lines(words)), "tables": 0, "review_required": bool(warnings), "warnings": warnings}
    try:
        import cv2
        import numpy as np
        if not hasattr(cv2, "imdecode") or not hasattr(np, "fromfile"):
            raise ImportError("Incomplete image dependencies")
    except ImportError:
        warnings.append("缺少 OpenCV/numpy，无法核验图片表格结构；已保留正文，需视觉复核。")
        result["review_required"] = True
        return result
    try:
        gray = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    except (OSError, cv2.error):
        gray = None
    if gray is None:
        warnings.append("原图读取失败，无法核验表格结构；已保留正文，需视觉复核。")
        result["review_required"] = True
        return result
    height, width = gray.shape
    # Local contrast preserves light one-pixel borders beside shaded headers.
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 31, 5)
    hk, vk = max(18, width // 45), max(18, height // 60)
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((1, hk), np.uint8))
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((vk, 1), np.uint8))
    joined = cv2.morphologyEx(horizontal | vertical, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    _, labels, stats, _ = cv2.connectedComponentsWithStats(joined)
    events, consumed = [], set()
    for label, (x, y, w, h, area) in enumerate(stats[1:], 1):
        if w < 40 or h < 30 or area < 120:
            continue
        component = labels[y:y + h, x:x + w] == label
        hs = horizontal[y:y + h, x:x + w] * component
        vs = vertical[y:y + h, x:x + w] * component
        xs = _axes(_longest_runs(vs), max(vk * 1.5, h * .18))
        ys = _axes(_longest_runs(hs.T), max(hk * 1.5, w * .18))
        if len(xs) < 3 or len(ys) < 3:
            continue
        try:
            if min(np.diff(xs)) < 8 or min(np.diff(ys)) < 8:
                raise ValueError("单元格尺寸不足以可靠判断")
            cells = _grid(hs, vs, xs, ys)
            selected = set()
            for index, word in enumerate(words):
                if not (x + xs[0] <= word["cx"] <= x + xs[-1] and y + ys[0] <= word["cy"] <= y + ys[-1]):
                    continue
                for cell in cells:
                    left, right = x + xs[cell["c0"]], x + xs[cell["c1"]]
                    top, bottom = y + ys[cell["r0"]], y + ys[cell["r1"]]
                    if left <= word["cx"] <= right and top <= word["cy"] <= bottom:
                        if word["x0"] < left - 3 or word["x1"] > right + 3 or word["y0"] < top - 3 or word["y1"] > bottom + 3:
                            raise ValueError("OCR 文字框跨越单元格边界")
                        cell["words"].append(word); selected.add(index)
                        break
            events.append((y + ys[0], x + xs[0], _render(cells, len(ys) - 1, len(xs) - 1)))
            consumed.update(selected)
            result["tables"] += 1
        except ValueError as exc:
            warnings.append(f"疑似表格（位置 {x},{y}）{exc}；保留 OCR 正文，需视觉复核。")
    remaining = [word for index, word in enumerate(words) if index not in consumed]
    if _possible_columns(remaining):
        warnings.append("检测到重复对齐的多列文字，可能为无边框表格；未猜测单元格，保留正文，需视觉复核。")
    events.extend((word["y0"], word["x0"], word["text"]) for word in remaining)
    result["markdown"] = "\n\n".join(event[2] for event in sorted(events))
    result["review_required"] = bool(warnings)
    return result
