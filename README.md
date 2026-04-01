# pdf2epub

Convert a PDF to a reflowable, text-rich EPUB — with embedded images, proper heading structure, a nested table of contents, and text that wraps freely at any window width.

Built to handle complex, design-heavy InDesign PDFs (multi-column layouts, dot-leader TOC pages, running headers/footers, embedded figures).

---

## Features

| Feature | Detail |
|---|---|
| **Reflowable text** | PDF layout lines are joined into paragraphs; no fixed widths |
| **Heading detection** | Font size + bold flags → `h1`–`h4` automatically |
| **TOC page** | Detects dot-leader and tab-separated entries; keeps each line separate |
| **Nested nav TOC** | Uses the PDF's own outline hierarchy for sidebar navigation |
| **Header/footer strip** | Removes running page numbers / chapter titles from content |
| **Image extraction** | Embeds all page images; converts non-PNG/JPEG formats via Pillow |
| **NCX fallback** | Writes `toc.ncx` for older readers (Kindle older firmware, etc.) |
| **Built-in auditor** | `audit.py` validates the output across 7 categories |

---

## Quick start

```bash
# 1. Set up the environment (one time)
bash setup.sh

# 2. Convert
.venv/bin/python convert.py my_book.pdf

# 3. (Optional) Audit the output
.venv/bin/python audit.py my_book_converted.epub
```

The EPUB is written to the same directory as the PDF, with the suffix `_converted.epub`. You can also specify a custom output path:

```bash
.venv/bin/python convert.py input.pdf output.epub
```

---

## Requirements

- Python 3.10+
- [PyMuPDF](https://pymupdf.readthedocs.io/) — PDF parsing
- [ebooklib](https://github.com/aerkalov/ebooklib) — EPUB creation
- [Pillow](https://pillow.readthedocs.io/) — image conversion
- [lxml](https://lxml.de/) — XHTML parsing (audit only)

Install all with:
```bash
pip install -r requirements.txt
```

---

## Files

| File | Purpose |
|---|---|
| `convert.py` | Main PDF → EPUB converter |
| `audit.py` | EPUB validator (7 checks) |
| `requirements.txt` | Python dependencies |
| `setup.sh` | One-shot venv + install script |

---

## How it works

### Text extraction
PyMuPDF's `get_text("dict")` returns text as a tree of blocks → lines → spans. All lines within a block are PDF layout wraps of the same paragraph, so they are joined with a space into a single `<p>` tag — enabling free reflow.

### TOC page detection
The visual Table of Contents page uses leader patterns like `. . . .` or tab-separated numbers. The detector handles:
- Spaced dot-leaders: `(?:.[ 	]*){3,}`
- Tab + page number: `\t\s*\d{1,4}`
- 2-line chapter blocks: title on line 0, bare number on line 1
- Roman-numeral page numbers

### Header / footer stripping
Blocks in the top or bottom 8 % of the page with fewer than 80 characters are treated as running headers/footers and excluded.

### Navigation TOC
The PDF's built-in outline (`doc.get_toc()`) provides `(level, title, page)` triples. These are converted into ebooklib's nested `(Section, [children])` structure so EPUB readers display a proper sidebar hierarchy.

---

## Limitations

- Multi-column body text may not reflow in perfect reading order (PDF doesn't encode column order)
- Tables are extracted as plain text (no `<table>` tags)
- Right-to-left scripts are untested

---

## License

MIT
