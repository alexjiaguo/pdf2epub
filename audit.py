#!/usr/bin/env python3
"""
epub_audit.py — Validate an EPUB file for common display and structural issues.

Checks
------
1. EPUB structure — container.xml, mimetype, OPF present
2. Metadata — title, author, language, cover
3. Spine & manifest — all referenced files exist in the zip
4. XHTML quality — parse errors, paragraph counts, reflow indicators, headings, TOC entries
5. Images — manifest vs zip, broken src refs, suspiciously tiny files
6. Navigation TOC — link count, nesting depth, broken hrefs
7. CSS — fixed-width patterns, inline font-size on body spans

Usage
-----
python audit.py book.epub
"""

import sys, os, re, zipfile
from lxml import etree

EPUB_PATH = sys.argv[1] if len(sys.argv) > 1 else None
if not EPUB_PATH:
    print("Usage: python audit.py book.epub")
    sys.exit(1)

NS = {
    "xhtml": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
    "opf": "http://www.idpf.org/2007/opf",
}
XHTML_NS = "http://www.w3.org/1999/xhtml"

issues = []
warnings = []
passed = []

def issue(msg): issues.append(f" ❌ {msg}")
def warn(msg): warnings.append(f" ⚠️ {msg}")
def ok(msg): passed.append(f" ✅ {msg}")

print(f"\n{'='*60}")
print(f" EPUB AUDIT: {os.path.basename(EPUB_PATH)}")
print(f"{'='*60}\n")

with zipfile.ZipFile(EPUB_PATH, "r") as z:
    names = set(z.namelist())

    # ── 1. Structure ──────────────────────────────────────────────────────────
    print("[ 1 / 7 ] EPUB structure")
    for f in ["META-INF/container.xml", "mimetype"]:
        (ok if f in names else issue)(f"{'Found' if f in names else 'Missing'}: {f}")

    container = z.read("META-INF/container.xml")
    root = etree.fromstring(container)
    opf_path = root.find(
        ".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile"
    ).get("full-path")
    ok(f"OPF path: {opf_path}")

    opf_root = etree.fromstring(z.read(opf_path))
    opf_dir = os.path.dirname(opf_path)

    # ── 2. Metadata ───────────────────────────────────────────────────────────
    print("\n[ 2 / 7 ] Metadata")
    DC = "http://purl.org/dc/elements/1.1/"
    title_el = opf_root.find(f".//{{{DC}}}title")
    author_els = opf_root.findall(f".//{{{DC}}}creator")
    lang_el = opf_root.find(f".//{{{DC}}}language")

    (ok if title_el is not None and title_el.text else issue)(
        f"Title: {title_el.text if title_el is not None else 'MISSING'}"
    )
    for a in author_els: ok(f"Author: {a.text}")
    if not author_els: warn("No <dc:creator> found")

    (ok if lang_el is not None and lang_el.text else warn)(
        f"Language: {lang_el.text if lang_el is not None else 'MISSING'}"
    )

    cover_meta = opf_root.find(".//{http://www.idpf.org/2007/opf}meta[@name='cover']")
    (ok if cover_meta is not None else warn)("Cover meta tag present" if cover_meta is not None else "No cover meta tag")

    # ── 3. Spine & manifest ───────────────────────────────────────────────────
    print("\n[ 3 / 7 ] Spine & manifest")
    manifest_items = {
        item.get("id"): item.get("href")
        for item in opf_root.findall(".//{http://www.idpf.org/2007/opf}item")
    }
    spine_idrefs = [
        item.get("idref")
        for item in opf_root.findall(".//{http://www.idpf.org/2007/opf}itemref")
    ]
    ok(f"Manifest items: {len(manifest_items)}")
    ok(f"Spine entries: {len(spine_idrefs)}")

    missing_in_manifest = [r for r in spine_idrefs if r not in manifest_items]
    if missing_in_manifest:
        issue(f"Spine idrefs not in manifest: {missing_in_manifest}")

    missing_files = [
        href for href in manifest_items.values()
        if os.path.join(opf_dir, href).lstrip("./") not in names and href not in names
    ]
    (ok if not missing_files else issue)(
        "All manifest files present in zip" if not missing_files
        else f"Missing files in zip ({len(missing_files)}): {missing_files[:5]}"
    )

    # ── 4. XHTML quality ──────────────────────────────────────────────────────
    print("\n[ 4 / 7 ] XHTML content quality")
    xhtml_items = [(mid, href) for mid, href in manifest_items.items() if href.endswith(".xhtml")]
    parser = etree.XMLParser(recover=True)

    total_paras = long_paras = short_paras = empty_paras = 0
    headings = {"h1": 0, "h2": 0, "h3": 0, "h4": 0}
    toc_entries = 0
    image_refs = []
    xhtml_errors = []
    span_fontsizes = 0

    for mid, href in sorted(xhtml_items):
        full = href if href in names else os.path.join(opf_dir, href).lstrip("./")
        try:
            raw = z.read(full)
            tree = etree.fromstring(raw, parser)
        except Exception as e:
            xhtml_errors.append(f"{href}: {e}")
            continue
        
        body = tree.find(f"{{{XHTML_NS}}}body")
        if body is None:
            xhtml_errors.append(f"{href}: no <body>")
            continue

        for p in body.iter(f"{{{XHTML_NS}}}p"):
            text = "".join(p.itertext()).strip()
            total_paras += 1
            if not text: empty_paras += 1
            elif len(text) > 300: long_paras += 1
            elif len(text) < 30: short_paras += 1
            
            if "toc-entry" in (p.get("class") or ""):
                toc_entries += 1

        for tag in headings:
            headings[tag] += len(body.findall(f".//{{{XHTML_NS}}}{tag}"))

        for img in body.iter(f"{{{XHTML_NS}}}img"):
            image_refs.append((href, img.get("src", "")))
        
        for span in body.iter(f"{{{XHTML_NS}}}span"):
            if "font-size" in (span.get("style") or ""):
                span_fontsizes += 1

    if xhtml_errors:
        for e in xhtml_errors[:5]: issue(f"XHTML error: {e}")
    else:
        ok(f"All {len(xhtml_items)} XHTML files parse without fatal errors")

    ok(f"Total paragraphs: {total_paras:,}")
    (ok if not empty_paras else warn)(
        f"Empty <p> tags: {empty_paras}" if empty_paras else "No empty paragraphs"
    )
    (ok if long_paras > 0 else warn)(
        f"Long reflowable paragraphs (>300 chars): {long_paras:,} — reflow confirmed ✓"
        if long_paras else "No long paragraphs found — text may not be reflowing"
    )
    (ok if short_paras < total_paras * 0.3 else warn)(
        f"Short paragraphs (<30 chars): {short_paras} — likely labels/headings, expected"
    )
    ok(f"Headings: " + " ".join(f"{t}={c}" for t, c in headings.items()))
    (ok if toc_entries > 0 else warn)(
        f"TOC content entries (toc-entry class): {toc_entries}"
        if toc_entries else "No toc-entry class found — TOC page detection may have missed entries"
    )
    (ok if span_fontsizes == 0 else warn)(
        "No inline font-size on spans ✓" if span_fontsizes == 0 
        else f"Inline font-size on {span_fontsizes} spans — may pin text width"
    )

    # ── 5. Images ─────────────────────────────────────────────────────────────
    print("\n[ 5 / 7 ] Images")
    image_items = [
        (mid, href) for mid, href in manifest_items.items()
        if any(href.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"))
    ]
    ok(f"Image files in manifest: {len(image_items)}")

    broken = []
    for ch_href, img_src in image_refs:
        ch_dir = os.path.dirname(os.path.join(opf_dir, ch_href))
        full_img = os.path.normpath(os.path.join(ch_dir, img_src)).replace("\\", "/")
        if full_img not in names and img_src.lstrip("./") not in names:
            alt = os.path.normpath(os.path.join(opf_dir, img_src)).replace("\\", "/").lstrip("./")
            if alt not in names:
                broken.append(img_src)
    
    (ok if not broken else issue)(
        f"All {len(image_refs)} image references resolve correctly" 
        if not broken else f"Broken image refs ({len(broken)}): {broken[:5]}"
    )

    tiny = []
    for mid, href in image_items:
        full = href if href in names else os.path.join(opf_dir, href).lstrip("./")
        try:
            if len(z.read(full)) < 500:
                tiny.append(href)
        except Exception: pass
    (ok if not tiny else warn)(
        "No suspiciously tiny images" if not tiny
        else f"Very small images (<500 B): {tiny[:3]}"
    )

    # ── 6. Navigation TOC ─────────────────────────────────────────────────────
    print("\n[ 6 / 7 ] Navigation TOC")
    nav_href = next(
        (href for mid, href in manifest_items.items() if "nav" in mid.lower() or href.endswith("nav.xhtml")),
        None
    )
    if nav_href:
        full_nav = nav_href if nav_href in names else os.path.join(opf_dir, nav_href).lstrip("./")
        try:
            nav_tree = etree.fromstring(z.read(full_nav), parser)
            nav_links = nav_tree.findall(f".//{{{XHTML_NS}}}a")
            nav_ols = nav_tree.findall(f".//{{{XHTML_NS}}}ol")
            ok(f"Nav TOC links: {len(nav_links)}")
            (ok if len(nav_ols) > 1 else warn)(
                f"Nav TOC has {len(nav_ols)} nested <ol> — hierarchy present ✓" 
                if len(nav_ols) > 1 else "Nav TOC appears flat (only 1 <ol>)"
            )
            broken_nav = [
                a.get("href", "").split("#")[0] for a in nav_links 
                if a.get("href", "").split("#")[0] and os.path.normpath(os.path.join(os.path.dirname(full_nav), a.get("href","").split("#")[0]) ).replace("\\","/").lstrip("./") not in names
            ]
            (ok if not broken_nav else issue)(
                "All nav TOC links resolve correctly" if not broken_nav
                else f"Broken nav links: {broken_nav[:5]}"
            )
        except Exception as e:
            warn(f"Could not parse nav file: {e}")
    else:
        warn("No nav.xhtml found")

    ncx = next((href for mid, href in manifest_items.items() if href.endswith(".ncx")), None)
    (ok if ncx else warn)(f"NCX toc present: {ncx}" if ncx else "No NCX toc (needed for older readers)")

    # ── 7. CSS ────────────────────────────────────────────────────────────────
    print("\n[ 7 / 7 ] CSS")
    css_items = [href for mid, href in manifest_items.items() if href.endswith(".css")]
    if css_items:
        ok(f"CSS files: {css_items}")
        for css_href in css_items:
            full_css = css_href if css_href in names else os.path.join(opf_dir, css_href).lstrip("./")
            try:
                css_content = z.read(full_css).decode("utf-8")
                fw = re.findall(r'width\s*:\s*\d+px', css_content)
                (ok if not fw else warn)(
                    "No fixed px widths in CSS — reflow-friendly ✓" 
                    if not fw else f"Fixed px widths found (may prevent reflow): {fw[:3]}"
                )
            except Exception as e:
                warn(f"Could not read CSS: {e}")
    else:
        warn("No CSS files found")

# ── Summary ───────────────────────────────────────────────────────────────────

print(f"\n{'='*60}")
print(" AUDIT SUMMARY")
print(f"{'='*60}")

if issues:
    print(f"\n🔴 ISSUES ({len(issues)}) — must fix:")
    for i in issues: print(i)

if warnings:
    print(f"\n🟡 WARNINGS ({len(warnings)}) — review:")
    for w in warnings: print(w)

print(f"\n🟢 PASSED ({len(passed)}):")
for p in passed: print(p)

print(f"\n{'='*60}")
print(" ✅ No critical issues found." if not issues else f" ❌ {len(issues)} issue(s) need fixing.")
print(f"{'='*60}\n")
