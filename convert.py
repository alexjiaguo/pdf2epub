#!/usr/bin/env python3
"""
pdf2epub — PDF to reflowable EPUB converter
Extracts real text (with bold/italic/colour) and embedded images from a PDF
and packages them into a valid EPUB 3 file that reflows at any window width.

Key features
------------
- Reflowable text: PDF layout lines are joined into paragraphs
- Heading detection: font-size / bold flags → h1–h4
- Table-of-contents page: detects dot-leader and tab-separated entries,
  keeps each line separate and styled (chapter vs. sub-entry)
- Nested nav TOC: uses the PDF's own outline hierarchy
- Header/footer stripping: removes running page numbers / chapter titles
- Image extraction: embeds all page images (converted to PNG if needed)
- NCX fallback: also writes toc.ncx for older readers

Usage
-----
python convert.py input.pdf [output.epub]
If output path is omitted, the EPUB is written next to the PDF with the
suffix _converted.epub.

Requirements (see requirements.txt)
------------------------------------
PyMuPDF, ebooklib, Pillow
"""

import sys
import os
import io
import re
import time
import hashlib
import fitz  # PyMuPDF
from ebooklib import epub
from PIL import Image

# ── Utilities ────────────────────────────────────────────────────────────────

def sanitize_html(text: str) -> str:
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))

def font_to_css(font_name: str, font_size: float, font_flags: int, color: int) -> str:
    """
    Return inline CSS for character-level styling.
    Intentionally omits font-size so the EPUB reader controls text scaling,
    which is what allows text to reflow freely at any window width.
    """
    styles = []
    is_bold = bool(font_flags & (1 << 18)) or bool(font_name and "Bold" in font_name)
    is_italic = bool(font_flags & (1 << 1)) or bool(font_name and ("Italic" in font_name or "Oblique" in font_name))
    is_mono = bool(font_flags & (1 << 0)) or bool(font_name and ("Mono" in font_name or "Courier" in font_name or "Code" in font_name))

    if is_bold: styles.append("font-weight: bold")
    if is_italic: styles.append("font-style: italic")
    if is_mono: styles.append("font-family: 'Courier New', monospace")
    if color and color != 0:
        r = (color >> 16) & 0xFF
        g = (color >> 8) & 0xFF
        b = color & 0xFF
        if not (r == 0 and g == 0 and b == 0):
            styles.append(f"color: #{r:02x}{g:02x}{b:02x}")
    return "; ".join(styles)

def classify_heading(font_size: float, font_flags: int, font_name: str, avg_size: float) -> str | None:
    """Map font metrics to heading levels. Returns 'h1'–'h4', 'strong', or None."""
    is_bold = bool(font_flags & (1 << 18)) or bool(font_name and "Bold" in font_name)
    if font_size >= avg_size * 2.0: return "h1"
    if font_size >= avg_size * 1.6: return "h2"
    if font_size >= avg_size * 1.3 and is_bold: return "h3"
    if font_size >= avg_size * 1.15 and is_bold: return "h4"
    if is_bold and font_size >= avg_size: return "strong"
    return None

def compute_avg_font_size(page) -> float:
    """Weighted average font size across all text on the page."""
    sizes = []
    for block in page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]:
        if block.get("type") != 0: continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if span.get("text", "").strip() and span.get("size", 0) > 0:
                    sizes.extend([span["size"]] * len(span["text"]))
    return sum(sizes) / len(sizes) if sizes else 11.0

# ── Image extraction ─────────────────────────────────────────────────────────

def extract_images_from_page(page, doc, extracted_images: dict) -> list:
    """
    Extract all embedded images from a PDF page.
    Images smaller than 20×20 px are ignored (icons / decorations).
    Non-PNG/JPEG formats are converted to PNG via Pillow.
    Returns a list of image records with position (bbox) for y-sorting.
    """
    images = []
    for img_info in page.get_images(full=True):
        xref = img_info[0]
        if xref not in extracted_images:
            try:
                base = doc.extract_image(xref)
                if not base: continue
                data, ext = base["image"], base["ext"]
                w, h = base.get("width", 0), base.get("height", 0)
                if w < 20 or h < 20: continue

                ext_map = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg"}
                media_type = ext_map.get(ext, "image/png")

                if ext not in ("png", "jpeg", "jpg"):
                    try:
                        pil = Image.open(io.BytesIO(data))
                        if pil.mode not in ("RGB", "L"): pil = pil.convert("RGB")
                        buf = io.BytesIO()
                        pil.save(buf, "PNG")
                        data, ext, media_type = buf.getvalue(), "png", "image/png"
                    except Exception: continue

                extracted_images[xref] = {
                    "image_id": f"img_{xref}",
                    "filename": f"images/img_{xref}.{ext}",
                    "media_type": media_type,
                    "data": data,
                    "width": w,
                    "height": h,
                }
            except Exception: continue

        if xref not in extracted_images: continue
        rec = extracted_images[xref].copy()
        try:
            rects = page.get_image_rects(img_info)
            rec["bbox"] = (
                (rects[0].x0, rects[0].y0, rects[0].x1, rects[0].y1) if rects
                else (0, 0, rec["width"], rec["height"])
            )
        except Exception:
            rec["bbox"] = (0, 0, rec["width"], rec["height"])
        images.append(rec)
    return images

# ── Header / footer stripping ─────────────────────────────────────────────────

def is_header_footer(block, page_height: float, page_width: float) -> bool:
    """
    Detect running headers and footers.
    They live in the top or bottom 8 % of the page, are at most 2 lines,
    and contain fewer than 80 characters (page number, chapter title).
    """
    bbox = block.get("bbox", (0, 0, page_width, page_height))
    y0, y1 = bbox[1], bbox[3]
    lines = block.get("lines", [])
    if len(lines) > 2: return False
    block_text = " ".join(
        s.get("text", "") for ln in lines for s in ln.get("spans", [])
    ).strip()
    return (y1 <= page_height * 0.08 or y0 >= page_height * 0.92) and len(block_text) < 80

# ── TOC block detection ───────────────────────────────────────────────────────
#
# The PDF table-of-contents page uses several leader / separator styles:
# A. Spaced dots: "Chapter 1. Something . . . . . . . 24"
# B. Tab separator: "Chapter 2.\t Using GenAI\t" + "53" on next line
# C. 2+ spaces before number: "Preface  vii"
# D. Roman-numeral page numbers: "Introduction  ix"
# E. Bare page number on its own line (part of style B)

_TOC_DOTS_RE   = re.compile(r'(?:\.[ \t]*){3,}') # ". . ." or "...." leaders
_TOC_TAB_RE    = re.compile(r'\t\s*\d{1,4}\s*$|\t\s*$') # tab then number at line end
_TOC_SPACES_RE = re.compile(r'[ \t]{2,}\d{1,4}\s*$') # 2+ spaces then number
_ROMAN_PAGE_RE = re.compile(r'[ \t]{2,}(x{0,3}(?:ix|iv|v?i{0,3}))\s*$', re.IGNORECASE)
_STANDALONE_NUM_RE = re.compile(r'^\s*\d{1,4}\s*$')

def _line_text(line: dict) -> str:
    return "".join(s.get("text", "") for s in line.get("spans", []))

def _line_is_toc(text: str) -> bool:
    return bool(
        _TOC_DOTS_RE.search(text) or
        _TOC_TAB_RE.search(text) or
        _TOC_SPACES_RE.search(text) or
        _ROMAN_PAGE_RE.search(text) or
        _STANDALONE_NUM_RE.match(text.strip())
    )

def is_toc_block(block: dict) -> bool:
    """
    Heuristic to decide if a text block is part of the TOC page.
    Handles two patterns found in the wild:
    - 2-line block: "Title\\t" on line 0, "53" alone on line 1
    - Multi-line block: 5–10 dot-leader entries (≥45 % must match)
    - Single-line: a standalone dot-leader entry
    """
    lines = block.get("lines", [])
    if not lines: return False
    line_texts = [_line_text(ln) for ln in lines]

    if len(lines) == 1:
        return bool(_TOC_DOTS_RE.search(line_texts[0]))

    if len(lines) == 2:
        return bool(
            _STANDALONE_NUM_RE.match(line_texts[-1].strip()) and
            (
                _TOC_TAB_RE.search(line_texts[0]) or
                _ROMAN_PAGE_RE.search(line_texts[0]) or
                _TOC_SPACES_RE.search(line_texts[0])
            )
        )

    hits = sum(1 for t in line_texts if _line_is_toc(t))
    return hits >= len(lines) * 0.45

def process_toc_block(block: dict, avg_font_size: float) -> str:
    """
    Render a TOC block as structured HTML.
    - 2-line blocks (chapter name + bare page number) → one bold line with em-dash
    - Multi-line blocks → one <p class="toc-entry"> per line
    """
    lines = block.get("lines", [])
    line_texts = [_line_text(ln) for ln in lines]
    parts = []

    # ── 2-line chapter heading ────────────────────────────────────────────────
    if len(lines) == 2 and _STANDALONE_NUM_RE.match(line_texts[-1].strip()):
        all_spans = [(s, ln) for ln in lines for s in ln.get("spans", [])]
        inline = []
        for span, _ in all_spans:
            t = span.get("text", "")
            if not t.strip(): continue
            cleaned_t = re.sub(r'\s*\t+\s*', ' ', t).strip()
            if not cleaned_t: continue
            escaped = sanitize_html(cleaned_t)
            css = font_to_css(
                span.get("font", ""), span.get("size", 12),
                span.get("flags", 0), span.get("color", 0)
            )
            if inline and _STANDALONE_NUM_RE.match(cleaned_t):
                inline.append(" \u2014 ") # em-dash separator before page number
            inline.append(f'<span style="{css}">{escaped}</span>' if css else escaped)
        
        joined = "".join(inline).strip()
        if joined:
            dominant = max(all_spans, key=lambda x: len(x[0].get("text", "")), default=({}, ""))[0]
            heading = classify_heading(
                dominant.get("size", 12), dominant.get("flags", 0),
                dominant.get("font", ""), avg_font_size
            )
            if heading and heading.startswith("h"):
                parts.append(f"<{heading}>{joined}</{heading}>")
            else:
                parts.append(f"<p class='toc-entry toc-chapter'>{joined}</p>")
        return "\n".join(parts)

    # ── Multi-line sub-entry list ─────────────────────────────────────────────
    for line in lines:
        spans = line.get("spans", [])
        inline = []
        for span in spans:
            text = span.get("text", "")
            if not text: continue
            # Replace spaced dot-leaders with a centred dot for cleaner display
            cleaned = re.sub(r'(?:\.[ \t]*){3,}', ' · ', text)
            escaped = sanitize_html(cleaned)
            css = font_to_css(
                span.get("font", ""), span.get("size", 12),
                span.get("flags", 0), span.get("color", 0)
            )
            inline.append(f'<span style="{css}">{escaped}</span>' if css else escaped)
        
        joined = "".join(inline).strip()
        if joined:
            dominant = max(spans, key=lambda s: len(s.get("text", "")), default={})
            heading = classify_heading(
                dominant.get("size", 12), dominant.get("flags", 0),
                dominant.get("font", ""), avg_font_size
            )
            if heading and heading.startswith("h"):
                parts.append(f"<{heading}>{joined}</{heading}>")
            else:
                parts.append(f"<p class='toc-entry'>{joined}</p>")
    return "\n".join(parts)

# ── Regular text block ────────────────────────────────────────────────────────

def process_text_block(block: dict, avg_font_size: float) -> str:
    """
    Convert a PDF text block to reflowable HTML.
    All PDF lines within a single block are layout-wraps of the same paragraph.
    We join them with a space into one <p> so the EPUB reader can reflow freely.
    TOC-style blocks are delegated to process_toc_block() to keep entries separate.
    """
    if is_toc_block(block):
        return process_toc_block(block, avg_font_size)

    lines = block.get("lines", [])
    if not lines: return ""
    all_spans = [s for ln in lines for s in ln.get("spans", [])]
    if not all_spans: return ""

    dominant = max(all_spans, key=lambda s: len(s.get("text", "")))
    block_heading = classify_heading(
        dominant.get("size", 12), dominant.get("flags", 0),
        dominant.get("font", ""), avg_font_size,
    )

    inline_parts = []
    for line in lines:
        line_parts = []
        for span in line.get("spans", []):
            text = span.get("text", "")
            if not text: continue
            escaped = sanitize_html(text)
            css = font_to_css(
                span.get("font", ""), span.get("size", 12),
                span.get("flags", 0), span.get("color", 0),
            )
            line_parts.append(f'<span style="{css}">{escaped}</span>' if css else escaped)
        
        if line_parts:
            line_html = "".join(line_parts)
            # Ensure words from adjacent lines don't merge (e.g. "word\nword")
            if inline_parts and not inline_parts[-1].endswith(" ") and not line_html.startswith(" "):
                inline_parts.append(" ")
            inline_parts.append(line_html)

    if not inline_parts: return ""
    joined = "".join(inline_parts).strip()
    if not joined: return ""

    if block_heading and block_heading.startswith("h"):
        return f"<{block_heading}>{joined}</{block_heading}>"
    if block_heading == "strong":
        return f"<p><strong>{joined}</strong></p>"
    return f"<p>{joined}</p>"

# ── Main conversion ───────────────────────────────────────────────────────────

CSS = b"""
body { font-family: Georgia, serif; line-height: 1.65; margin: 1em; color: #1a1a1a; background: #fff; }
h1 { font-size: 1.8em; margin: 1.2em 0 0.5em; color: #111; page-break-after: avoid; }
h2 { font-size: 1.45em; margin: 1em 0 0.4em; color: #222; page-break-after: avoid; }
h3 { font-size: 1.2em; margin: 0.8em 0 0.3em; color: #333; page-break-after: avoid; }
h4 { font-size: 1.05em; margin: 0.6em 0 0.3em; page-break-after: avoid; }
p { margin: 0.35em 0; text-align: justify; orphans: 2; widows: 2; }
p.toc-entry { margin: 0.15em 0 0.15em 1em; text-align: left; }
p.toc-chapter { margin: 0.5em 0 0.1em 0; text-align: left; font-weight: bold; }
img { max-width: 100%; height: auto; display: block; margin: 0.8em auto; page-break-inside: avoid; }
.figure { text-align: center; margin: 1em 0; page-break-inside: avoid; }
"""

def convert(pdf_path: str, epub_path: str) -> None:
    print(f"\nOpening: {pdf_path}")
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    meta = doc.metadata
    title = meta.get("title", "") or os.path.splitext(os.path.basename(pdf_path))[0]
    author = meta.get("author", "Unknown")
    print(f"Pages: {total_pages} | Title: {title} | Author: {author}")

    toc_entries = doc.get_toc()
    print(f"PDF outline entries: {len(toc_entries)}")

    # ── EPUB book object ──────────────────────────────────────────────────────
    book = epub.EpubBook()
    book.set_identifier("pdf2epub-" + hashlib.md5(pdf_path.encode()).hexdigest()[:12])
    book.set_title(title)
    book.set_language("en")
    for a in [x.strip() for x in author.split(";") if x.strip()]:
        book.add_author(a)

    css_item = epub.EpubItem(
        uid="style", file_name="style/default.css",
        media_type="text/css", content=CSS,
    )
    book.add_item(css_item)

    # ── Chapter boundaries from PDF outline ───────────────────────────────────
    toc_page_map: dict[int, str] = {}
    for lv, t, pg in toc_entries:
        if (pg - 1) not in toc_page_map:
            toc_page_map[pg - 1] = t

    chapter_boundaries: list[dict] = []
    cur_title, cur_pages = "Front Matter", []
    for pg in range(total_pages):
        if pg in toc_page_map:
            if cur_pages:
                chapter_boundaries.append({"title": cur_title, "pages": cur_pages})
            cur_title = toc_page_map[pg]
            cur_pages = [pg]
        else:
            cur_pages.append(pg)
    if cur_pages:
        chapter_boundaries.append({"title": cur_title, "pages": cur_pages})
    
    print(f"Chapters: {len(chapter_boundaries)}")

    # ── Process pages ─────────────────────────────────────────────────────────
    extracted_images: dict = {}
    added_images: set = set()
    epub_chapters: list = []
    cover_set = False
    start = time.time()

    for ch_idx, ch in enumerate(chapter_boundaries):
        parts = []
        for page_num in ch["pages"]:
            page = doc[page_num]
            page_h = page.rect.height
            page_w = page.rect.width
            avg_fs = compute_avg_font_size(page)
            text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
            text_blocks = [
                b for b in text_dict.get("blocks", [])
                if b.get("type") == 0 and not is_header_footer(b, page_h, page_w)
            ]
            for b in text_blocks:
                b["y0"] = b.get("bbox", (0, 0, 0, 0))[1]

            page_images = extract_images_from_page(page, doc, extracted_images)
            for img in page_images:
                iid = img["image_id"]
                if iid not in added_images:
                    book.add_item(epub.EpubItem(
                        uid=iid, file_name=img["filename"],
                        media_type=img["media_type"], content=img["data"],
                    ))
                    added_images.add(iid)
                if not cover_set and img["width"] > 200 and img["height"] > 200:
                    book.set_cover("cover." + img["filename"].split(".")[-1], img["data"])
                    cover_set = True

            # Merge text blocks and images by vertical position for correct order
            content = (
                [{"type": "text", "y": b["y0"], "data": b} for b in text_blocks] +
                [{"type": "image", "y": img["bbox"][1], "data": img} for img in page_images]
            )
            content.sort(key=lambda c: c["y"])

            for item in content:
                if item["type"] == "text":
                    html = process_text_block(item["data"], avg_fs)
                    if html.strip(): parts.append(html)
                else:
                    img = item["data"]
                    parts.append(f'<div class="figure"><img src="{img["filename"]}" alt="Figure"/></div>')

            done = page_num + 1
            eta = (time.time() - start) / done * (total_pages - done)
            print(f"\rPage {done}/{total_pages} ({done * 100 // total_pages}%) ETA:{eta:.0f}s", end="", flush=True)

        xhtml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<!DOCTYPE html>'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
            f'<head><title>{sanitize_html(ch["title"])}</title>'
            '<link rel="stylesheet" type="text/css" href="style/default.css"/></head>'
            f'<body>{"".join(parts)}</body></html>'
        )
        chap = epub.EpubHtml(
            title=ch["title"],
            file_name=f"chapter_{ch_idx + 1:03d}.xhtml",
            lang="en",
        )
        chap.content = xhtml.encode("utf-8")
        chap.add_item(css_item)
        book.add_item(chap)
        epub_chapters.append(chap)

    print(f"\nDone in {time.time() - start:.1f}s | Images embedded: {len(added_images)}")

    # ── Build nested navigation TOC ───────────────────────────────────────────
    ch_map: dict[int, int] = {}
    for ch_idx2, ch_info2 in enumerate(chapter_boundaries):
        for pg2 in ch_info2["pages"]:
            ch_map[pg2] = ch_idx2

    def make_toc_tree(entries: list) -> list:
        """Convert flat PDF outline levels into nested ebooklib TOC structure."""
        tree: list = []
        stack: list = [] # (level, list, idx, title, chap, children)
        seen: set = set()
        for level, et_title, pg_num in entries:
            pg_idx = pg_num - 1
            ch_idx2 = ch_map.get(pg_idx)
            if ch_idx2 is None or ch_idx2 >= len(epub_chapters): continue
            
            chap = epub_chapters[ch_idx2]
            link = epub.Link(chap.file_name, et_title, f"toc-{ch_idx2}-{level}")
            
            while stack and stack[-1][0] >= level:
                stack.pop()
            
            if not stack:
                if ch_idx2 not in seen:
                    tree.append(link)
                    seen.add(ch_idx2)
                children: list = []
                stack.append((level, tree, len(tree) - 1, et_title, chap, children))
            else:
                _, parent_list, parent_idx, parent_title, parent_chap, siblings = stack[-1]
                existing = parent_list[parent_idx]
                if isinstance(existing, epub.Link):
                    parent_list[parent_idx] = (
                        epub.Section(parent_title, href=parent_chap.file_name),
                        siblings,
                    )
                siblings.append(link)
                if ch_idx2 not in seen: seen.add(ch_idx2)
                stack.append((level, siblings, len(siblings) - 1, et_title, chap, []))
        return tree

    book.toc = make_toc_tree(toc_entries) if toc_entries else epub_chapters
    book.spine = ["nav"] + epub_chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    print(f"Writing EPUB → {epub_path}")
    epub.write_epub(epub_path, book, {})
    sz = os.path.getsize(epub_path)
    print(f"\n✅ Done! Size: {sz / 1_048_576:.1f} MB | "
          f"Chapters: {len(epub_chapters)} | Images: {len(added_images)}\n")
    doc.close()

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python convert.py input.pdf [output.epub]")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    epub_path = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(pdf_path)[0] + "_converted.epub"
    convert(pdf_path, epub_path)
