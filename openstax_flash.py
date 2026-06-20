#!/usr/bin/env python3
"""
openstax-flash — Flashcard generator from OpenStax Key Terms / Glossary pages.

Fetches chapter glossary pages from openstax.org and exports Anki-ready flashcards.
Stdlib only; no external dependencies.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import re
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USER_AGENT = "openstax-flash/1.0 (+https://github.com/Un0nnn/openstax-flash; educational, non-commercial)"
CMS_BOOKS_URL = "https://openstax.org/apps/cms/api/books/?format=json"
DEFAULT_CACHE = Path.home() / ".cache" / "openstax-flash"
CHAPTER_PAGE_RE = re.compile(r"^(\d+)-(?:key-terms|glossary|summary)$")
STRIP_HTML_RE = re.compile(r"<[^>]+>")
MATH_BLOCK_RE = re.compile(r"<math\b[^>]*>.*?</math>", re.DOTALL | re.IGNORECASE)

GREEK_MAP = {
    "α": r"\alpha",
    "β": r"\beta",
    "γ": r"\gamma",
    "δ": r"\delta",
    "ε": r"\varepsilon",
    "θ": r"\theta",
    "λ": r"\lambda",
    "μ": r"\mu",
    "π": r"\pi",
    "ρ": r"\rho",
    "σ": r"\sigma",
    "φ": r"\phi",
    "ω": r"\omega",
    "Δ": r"\Delta",
    "Σ": r"\Sigma",
    "Ω": r"\Omega",
    "∞": r"\infty",
}

OP_MAP = {
    "→": r"\to",
    "←": r"\leftarrow",
    "±": r"\pm",
    "×": r"\times",
    "÷": r"\div",
    "≤": r"\le",
    "≥": r"\ge",
    "≠": r"\ne",
    "≈": r"\approx",
    "∑": r"\sum",
    "∫": r"\int",
    "−": "-",
    "·": r"\cdot",
}


@dataclass(frozen=True)
class BookMeta:
    slug: str
    title: str
    subjects: list[str]
    rex_entry_url: str | None
    webview_link: str | None = None


@dataclass
class Flashcard:
    term: str
    definition: str
    chapter_num: int | None
    chapter_title: str
    page_slug: str
    page_kind: str
    book_slug: str
    tags: list[str] = field(default_factory=list)
    definition_latex: str = ""


@dataclass
class ExtractResult:
    book: BookMeta
    cards: list[Flashcard]
    chapters_fetched: int
    pages_skipped: int
    pages_total: int
    elapsed_s: float


@dataclass(frozen=True)
class TermRef:
    term: str
    page_slug: str
    fragment: str


# ---------------------------------------------------------------------------
# MathML → LaTeX / Unicode
# ---------------------------------------------------------------------------


def _local_tag(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _mathml_text(elem: ET.Element) -> str:
    parts = [elem.text or ""]
    for child in elem:
        parts.append(_mathml_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _mathml_to_unicode(math_html: str) -> str:
    inner = MATH_BLOCK_RE.search(math_html)
    if not inner:
        return strip_html(math_html)
    xml = re.sub(r"<annotation-xml[^>]*>.*?</annotation-xml>", "", inner.group(0), flags=re.DOTALL | re.I)
    xml = re.sub(r"</?semantics>", "", xml)
    xml = re.sub(r"<math[^>]*>|</math>", "", xml, flags=re.I)
    try:
        root = ET.fromstring(f"<root>{xml}</root>")
    except ET.ParseError:
        return normalize_ws(strip_html(math_html))
    return normalize_ws(_mathml_text(root))


def _join_math_parts(parts: list[str]) -> str:
    """Join MathML row fragments with spacing where juxtaposition is ambiguous."""
    out = ""
    for i, part in enumerate(parts):
        if not part:
            continue
        if out and (
            (out[-1].isalnum() or out[-1] in ")}]$")
            and (part[0].isalnum() or part[0] in "({[$\\")
        ):
            out += " "
        out += part
    return out


def _convert_mathml_elem(elem: ET.Element) -> str:
    tag = _local_tag(elem.tag)
    children = list(elem)

    if tag == "mtext":
        return (elem.text or "").strip()

    if tag == "mi":
        t = _mathml_text(elem).strip()
        if len(t) == 1 and t in GREEK_MAP:
            return GREEK_MAP[t]
        if len(t) > 1 and t.isalpha():
            return rf"\text{{{t}}}"
        return t

    if tag == "mn":
        return _mathml_text(elem).strip()

    if tag == "mo":
        op = _mathml_text(elem).strip()
        mapped = OP_MAP.get(op, op)
        if mapped in (r"\to", r"\pm", r"\cdot", r"\times", "=", "<", ">", r"\le", r"\ge", r"\ne"):
            return f" {mapped} "
        return mapped

    if tag == "mrow":
        parts = [_convert_mathml_elem(c) for c in children]
        return _join_math_parts(parts)

    if tag == "mfrac" and len(children) >= 2:
        return rf"\frac{{{_convert_mathml_elem(children[0])}}}{{{_convert_mathml_elem(children[1])}}}"

    if tag == "msup" and len(children) >= 2:
        base = _convert_mathml_elem(children[0])
        sup = _convert_mathml_elem(children[1])
        if sup in ("+", "-"):
            return f"{base}^{sup}"
        return f"{base}^{{{sup}}}"

    if tag == "msub" and len(children) >= 2:
        base = _convert_mathml_elem(children[0])
        sub = _convert_mathml_elem(children[1])
        return f"{base}_{{{sub}}}"

    if tag == "munder" and len(children) >= 2:
        base = _convert_mathml_elem(children[0]).strip()
        sub = normalize_ws(_convert_mathml_elem(children[1]))
        if "lim" in base.lower():
            return rf"\lim_{{{sub}}}"
        return rf"\underset{{{sub}}}{{{base}}}"

    if tag == "msqrt" and children:
        return rf"\sqrt{{{_convert_mathml_elem(children[0])}}}"

    if tag == "mroot" and len(children) >= 2:
        return rf"\sqrt[{_convert_mathml_elem(children[0])}]{{{_convert_mathml_elem(children[1])}}}"

    if children:
        return "".join(_convert_mathml_elem(c) for c in children)
    return elem.text or ""


def mathml_to_latex(math_html: str) -> str:
    xml = re.sub(r"<annotation-xml[^>]*>.*?</annotation-xml>", "", math_html, flags=re.DOTALL | re.I)
    xml = re.sub(r"</?semantics>", "", xml)
    xml = re.sub(r"<math[^>]*>|</math>", "", xml, flags=re.I)
    try:
        root = ET.fromstring(f"<root>{xml}</root>")
    except ET.ParseError:
        return _mathml_to_unicode(math_html)
    return "".join(_convert_mathml_elem(c) for c in root)


def html_to_text(fragment: str, *, use_latex: bool = False) -> str:
    """Convert glossary HTML to plain text or LaTeX-marked text."""

    def replace_math(match: re.Match[str]) -> str:
        block = match.group(0)
        if use_latex:
            latex = mathml_to_latex(block)
            return f"${latex}$"
        return _mathml_to_unicode(block)

    text = MATH_BLOCK_RE.sub(replace_math, fragment)
    text = re.sub(r"<em[^>]*>(.*?)</em>", r"\1", text, flags=re.DOTALL | re.I)
    text = re.sub(r"<strong[^>]*>(.*?)</strong>", r"\1", text, flags=re.DOTALL | re.I)
    text = re.sub(r"<sub[^>]*>(.*?)</sub>", r"_\1", text, flags=re.DOTALL | re.I)
    text = re.sub(r"<sup[^>]*>(.*?)</sup>", r"^\1", text, flags=re.DOTALL | re.I)
    text = STRIP_HTML_RE.sub(" ", text)
    text = html.unescape(text)
    return normalize_ws(text)


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def strip_html(text: str) -> str:
    return normalize_ws(STRIP_HTML_RE.sub(" ", html.unescape(text or "")))


def normalize_slug(slug: str) -> str:
    slug = slug.strip().strip("/")
    if slug.startswith("books/"):
        slug = slug[len("books/") :]
    return slug


# ---------------------------------------------------------------------------
# HTTP / book discovery
# ---------------------------------------------------------------------------


def http_get(url: str, cache_dir: Path | None, ttl_s: int = 86400) -> str:
    cache_path: Path | None = None
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(url.encode()).hexdigest()
        cache_path = cache_dir / f"{key}.html"
        if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < ttl_s:
            return cache_path.read_text(encoding="utf-8", errors="replace")

    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json"})
    try:
        with urlopen(req, timeout=45) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc

    if cache_path is not None:
        cache_path.write_text(body, encoding="utf-8")
    return body


def http_get_json(url: str, cache_dir: Path | None, ttl_s: int = 86400) -> Any:
    cache_path: Path | None = None
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(url.encode()).hexdigest()
        cache_path = cache_dir / f"{key}.json"
        if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < ttl_s:
            return json.loads(cache_path.read_text(encoding="utf-8"))

    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Failed to fetch JSON {url}: {exc}") from exc

    if cache_path is not None:
        cache_path.write_text(json.dumps(data), encoding="utf-8")
    return data


def parse_preloaded_state(page_html: str) -> dict[str, Any]:
    marker = "window.__PRELOADED_STATE__"
    idx = page_html.find(marker)
    if idx < 0:
        raise ValueError("missing __PRELOADED_STATE__ (not a Rex SSR page)")

    brace = page_html.index("{", page_html.index("=", idx))
    depth = 0
    for i in range(brace, len(page_html)):
        ch = page_html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(page_html[brace : i + 1])
    raise ValueError("could not parse __PRELOADED_STATE__ JSON")


def load_books_index(cache_dir: Path | None) -> list[BookMeta]:
    data = http_get_json(CMS_BOOKS_URL, cache_dir)
    books: list[BookMeta] = []
    for raw in data.get("books", []):
        slug = normalize_slug(raw.get("slug", ""))
        if not slug:
            continue
        rex = raw.get("webview_rex_link") or None
        if rex and not rex.startswith("http"):
            rex = f"https://openstax.org{rex}"
        if raw.get("book_state") not in (None, "live", "new_edition_available"):
            continue
        books.append(
            BookMeta(
                slug=slug,
                title=raw.get("title", slug),
                subjects=list(raw.get("subjects") or []),
                rex_entry_url=rex,
                webview_link=raw.get("webview_link") or None,
            )
        )
    return sorted(books, key=lambda b: b.title.lower())


def resolve_book(slug: str, cache_dir: Path | None) -> BookMeta:
    slug = normalize_slug(slug)
    books = load_books_index(cache_dir)
    for book in books:
        if book.slug == slug:
            return book
    matches = [b for b in books if slug in b.slug]
    if len(matches) == 1:
        return matches[0]
    if matches:
        names = ", ".join(b.slug for b in matches[:8])
        raise SystemExit(f"Ambiguous slug '{slug}'. Matches: {names}")
    raise SystemExit(f"Unknown book slug '{slug}'. Run: openstax_flash.py list")


def entry_urls_for_book(book: BookMeta) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add(url: str | None) -> None:
        if url and url not in seen:
            seen.add(url)
            urls.append(url)

    add(book.rex_entry_url)
    add(f"https://openstax.org/books/{book.slug}/pages/1-introduction")
    add(f"https://openstax.org/books/{book.slug}/pages/preface")
    return urls


def load_book_tree(book: BookMeta, cache_dir: Path | None) -> tuple[dict[str, Any], dict[str, Any]]:
    errors: list[str] = []
    for url in entry_urls_for_book(book):
        try:
            page_html = http_get(url, cache_dir)
            state = parse_preloaded_state(page_html)
            book_data = (state.get("content") or {}).get("book") or {}
            tree = book_data.get("tree")
            if tree:
                return book_data, tree
            errors.append(f"{url}: empty tree")
        except Exception as exc:
            errors.append(f"{url}: {exc}")

    detail = "\n".join(f"  - {e}" for e in errors)
    raise RuntimeError(
        f"Could not load book tree for '{book.slug}'. Tried: {'; '.join(errors)}"
    )


@dataclass(frozen=True)
class TermPage:
    slug: str
    title: str
    chapter_title: str
    chapter_num: int | None
    kind: str


def discover_term_pages(tree: dict[str, Any]) -> list[TermPage]:
    pages: list[TermPage] = []

    def walk(node: dict[str, Any], chapter_title: str = "") -> None:
        title_html = node.get("title") or ""
        title = strip_html(title_html)
        slug = (node.get("slug") or "").strip()
        slug_l = slug.lower()

        if re.search(r'class="os-number">\d+', title_html):
            chapter_title = title

        chapter_num: int | None = None
        m = CHAPTER_PAGE_RE.match(slug_l)
        if m:
            chapter_num = int(m.group(1))

        is_key_terms = slug_l.endswith("-key-terms") or title.lower() == "key terms"
        is_glossary = bool(m) and slug_l.endswith("-glossary")
        is_summary = bool(m) and slug_l.endswith("-summary")

        if is_glossary and chapter_num is None:
            is_glossary = False

        if is_key_terms or is_glossary or is_summary:
            kind = "summary" if is_summary else ("glossary" if is_glossary else "key-terms")
            pages.append(
                TermPage(
                    slug=slug,
                    title=title or slug,
                    chapter_title=chapter_title,
                    chapter_num=chapter_num,
                    kind=kind,
                )
            )

        for child in node.get("contents") or []:
            walk(child, chapter_title)

    walk(tree)
    pages.sort(key=lambda p: (p.chapter_num or 9999, p.slug))
    return pages


PAGE_KIND_PRIORITY = {"key-terms": 0, "glossary": 1, "summary": 2}


def prefer_term_pages(pages: list[TermPage]) -> list[TermPage]:
    """
    Keep one term page per chapter, preferring key-terms > glossary > summary.
    Pages without a chapter number are kept as-is (appendices, etc.).
    """
    by_chapter: dict[int, TermPage] = {}
    extras: list[TermPage] = []
    for page in pages:
        if page.chapter_num is None:
            extras.append(page)
            continue
        current = by_chapter.get(page.chapter_num)
        if current is None or PAGE_KIND_PRIORITY[page.kind] < PAGE_KIND_PRIORITY[current.kind]:
            by_chapter[page.chapter_num] = page
    merged = sorted(by_chapter.values(), key=lambda p: p.chapter_num or 9999)
    merged.extend(extras)
    return merged


def pick_sample_page(pages: list[TermPage]) -> TermPage:
    preferred = prefer_term_pages(pages)
    for kind in ("key-terms", "glossary", "summary"):
        matches = [p for p in preferred if p.kind == kind]
        if matches:
            return matches[len(matches) // 2]
    return preferred[len(preferred) // 2]


# ---------------------------------------------------------------------------
# Content parsers
# ---------------------------------------------------------------------------


def extract_main_html(page_html: str) -> str:
    m = re.search(r"<main[^>]*>(.*)</main>", page_html, re.DOTALL | re.IGNORECASE)
    return m.group(1) if m else page_html


def parse_glossary_dl(page_html: str) -> list[tuple[str, str, str]]:
    """Return (term, definition_plain, definition_latex) from <dl> blocks."""
    main = extract_main_html(page_html)
    results: list[tuple[str, str, str]] = []
    for block in re.findall(r"<dl\b[^>]*>(.*?)</dl>", main, re.DOTALL | re.IGNORECASE):
        for dt_dd in re.findall(
            r"<dt\b[^>]*>(.*?)</dt>\s*<dd\b[^>]*>(.*?)</dd>",
            block,
            re.DOTALL | re.IGNORECASE,
        ):
            term = html_to_text(dt_dd[0])
            if not term:
                continue
            plain = html_to_text(dt_dd[1], use_latex=False)
            latex = html_to_text(dt_dd[1], use_latex=True)
            results.append((term, plain, latex))
    return results


def parse_key_terms_index(page_html: str) -> list[TermRef]:
    """Organic-chemistry style: linked term list in section.key-terms."""
    main = extract_main_html(page_html)
    refs: list[TermRef] = []
    for section in re.findall(
        r'<section[^>]*\bclass="[^"]*key-terms[^"]*"[^>]*>(.*?)</section>',
        main,
        re.DOTALL | re.IGNORECASE,
    ):
        for m in re.finditer(
            r'<a\b[^>]*data-page-slug="([^"]+)"[^>]*data-page-fragment="([^"]*)"[^>]*>(.*?)</a>',
            section,
            re.DOTALL | re.IGNORECASE,
        ):
            slug, fragment, inner = m.group(1), m.group(2), m.group(3)
            term = html_to_text(inner)
            if term and slug:
                refs.append(TermRef(term=term, page_slug=slug, fragment=fragment or ""))
    return refs


def extract_sentence(text: str, term: str) -> str:
    """Return the sentence in text that contains term (case-insensitive)."""
    needle = term.split("(")[0].strip()
    if not needle:
        return text
    lower = text.lower()
    idx = lower.find(needle.lower())
    if idx < 0:
        return text
    start = max(text.rfind(".", 0, idx), text.rfind("?", 0, idx), text.rfind("!", 0, idx))
    start = 0 if start < 0 else start + 1
    end_candidates = [text.find(c, idx) for c in ".?!"] + [len(text)]
    end = min(x for x in end_candidates if x >= 0)
    return normalize_ws(text[start:end])


def parse_inline_term_definitions(page_html: str) -> dict[str, tuple[str, str, str]]:
    """
    Parse span[data-type=term] definitions from section pages.
    Returns fragment_id -> (term, plain_def, latex_def).
    """
    main = extract_main_html(page_html)
    found: dict[str, tuple[str, str, str]] = {}

    for para in re.findall(r"<p\b[^>]*>(.*?)</p>", main, re.DOTALL | re.IGNORECASE):
        para_plain = html_to_text(para)
        para_latex = html_to_text(para, use_latex=True)
        for m in re.finditer(
            r'<span\s+data-type="term"\s+id="([^"]+)"[^>]*>(.*?)</span>',
            para,
            re.DOTALL | re.IGNORECASE,
        ):
            frag_id, term_html = m.group(1), m.group(2)
            term = html_to_text(term_html)
            if not term:
                continue
            plain = extract_sentence(para_plain, term)
            latex = extract_sentence(para_latex, term)
            found[frag_id] = (term, plain, latex)

    return found


def parse_summary_bullets(page_html: str) -> list[tuple[str, str, str]]:
    """Microbiology-style chapter summaries with bolded key terms in bullets."""
    main = extract_main_html(page_html)
    results: list[tuple[str, str, str]] = []
    for section in re.findall(
        r'<section[^>]*\bclass="summary"[^>]*>(.*?)</section>',
        main,
        re.DOTALL | re.IGNORECASE,
    ):
        for li in re.findall(r"<li\b[^>]*>(.*?)</li>", section, re.DOTALL | re.IGNORECASE):
            strong = re.search(r"<strong\b[^>]*>(.*?)</strong>", li, re.DOTALL | re.IGNORECASE)
            if not strong:
                continue
            term = html_to_text(strong.group(1))
            full = html_to_text(li)
            if not term or not full:
                continue
            plain = full
            latex = html_to_text(li, use_latex=True)
            results.append((term, plain, latex))
    return results


def parse_section_key_terms_table(page_html: str) -> list[str]:
    main = extract_main_html(page_html)
    terms: list[str] = []
    for table in re.findall(
        r'<table[^>]*\bkey-terms\b[^>]*>.*?</table>',
        main,
        re.DOTALL | re.IGNORECASE,
    ):
        for entry in re.findall(r"<entry[^>]*>([^<]+)</entry>", table, re.IGNORECASE):
            term = normalize_ws(entry)
            if term:
                terms.append(term)
    return terms


def resolve_linked_terms(
    book_slug: str,
    refs: list[TermRef],
    cache_dir: Path | None,
) -> list[tuple[str, str, str]]:
    by_page: dict[str, list[TermRef]] = {}
    for ref in refs:
        by_page.setdefault(ref.page_slug, []).append(ref)

    resolved: list[tuple[str, str, str]] = []
    for page_slug, page_refs in by_page.items():
        url = f"https://openstax.org/books/{book_slug}/pages/{page_slug}"
        page_html = http_get(url, cache_dir)
        defs = parse_inline_term_definitions(page_html)
        by_term = {t.lower(): (t, p, l) for t, p, l in defs.values()}
        for ref in page_refs:
            if ref.fragment and ref.fragment in defs:
                resolved.append(defs[ref.fragment])
            elif ref.term.lower() in by_term:
                resolved.append(by_term[ref.term.lower()])
            else:
                resolved.append((ref.term, ref.term, ref.term))
    return resolved


def parse_term_page(
    page_html: str,
    book_slug: str,
    cache_dir: Path | None,
    include_section_tables: bool,
) -> list[tuple[str, str, str]]:
    pairs = parse_glossary_dl(page_html)
    if pairs:
        return pairs

    refs = parse_key_terms_index(page_html)
    if refs:
        return resolve_linked_terms(book_slug, refs, cache_dir)

    pairs = parse_summary_bullets(page_html)
    if pairs:
        return pairs

    if include_section_tables:
        return [(t, "(definition in chapter glossary)", t) for t in parse_section_key_terms_table(page_html)]

    return []


def make_card(
    book_slug: str,
    page: TermPage,
    term: str,
    definition: str,
    definition_latex: str,
    page_kind_override: str | None = None,
) -> Flashcard:
    chapter_label = strip_html(page.chapter_title) or f"Chapter {page.chapter_num or '?'}"
    kind = page_kind_override or page.kind
    tags = [book_slug.replace("-", "_")]
    if page.chapter_num is not None:
        tags.append(f"ch{page.chapter_num}")
    tags.append(kind.replace("-", "_"))
    tags.append("openstax")
    return Flashcard(
        term=term,
        definition=definition,
        definition_latex=definition_latex or definition,
        chapter_num=page.chapter_num,
        chapter_title=chapter_label,
        page_slug=page.slug,
        page_kind=kind,
        book_slug=book_slug,
        tags=tags,
    )


def fetch_term_page(
    book_slug: str,
    page: TermPage,
    cache_dir: Path | None,
    include_section_tables: bool,
) -> list[Flashcard]:
    url = f"https://openstax.org/books/{book_slug}/pages/{page.slug}"
    page_html = http_get(url, cache_dir)
    pairs = parse_term_page(page_html, book_slug, cache_dir, include_section_tables)
    return [
        make_card(book_slug, page, term, plain, latex)
        for term, plain, latex in pairs
    ]


def filter_chapters(pages: list[TermPage], chapters: set[int] | None) -> list[TermPage]:
    if not chapters:
        return pages
    return [p for p in pages if p.chapter_num is not None and p.chapter_num in chapters]


def extract_flashcards(
    book_slug: str,
    *,
    chapters: set[int] | None = None,
    cache_dir: Path | None = DEFAULT_CACHE,
    concurrency: int = 8,
    include_section_tables: bool = False,
    verbose: bool = False,
) -> ExtractResult:
    started = time.perf_counter()
    book = resolve_book(book_slug, cache_dir)
    try:
        _, tree = load_book_tree(book, cache_dir)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    all_pages = prefer_term_pages(discover_term_pages(tree))
    pages = filter_chapters(all_pages, chapters)

    if not all_pages:
        raise SystemExit(
            f"No Key Terms / Glossary / Summary pages found for '{book.slug}'."
        )
    if not pages:
        raise SystemExit(
            f"No term pages match chapter filter for '{book.slug}'. "
            f"Available chapters: {sorted({p.chapter_num for p in all_pages if p.chapter_num})}"
        )

    if verbose:
        print(f"Book: {book.title} ({book.slug})", file=sys.stderr)
        print(f"Term pages: {len(pages)} of {len(all_pages)}", file=sys.stderr)

    all_cards: list[Flashcard] = []
    skipped = 0

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {
            pool.submit(fetch_term_page, book.slug, page, cache_dir, include_section_tables): page
            for page in pages
        }
        for future in as_completed(futures):
            page = futures[future]
            try:
                cards = future.result()
                if cards:
                    all_cards.extend(cards)
                    if verbose:
                        print(
                            f"  ch{page.chapter_num}: {len(cards)} terms "
                            f"({page.kind}, {page.slug})",
                            file=sys.stderr,
                        )
                else:
                    skipped += 1
                    if verbose:
                        print(f"  ch{page.chapter_num}: 0 terms ({page.slug})", file=sys.stderr)
            except Exception as exc:
                skipped += 1
                print(f"Warning: {page.slug}: {exc}", file=sys.stderr)

    # Deduplicate same term in same chapter (keep longest definition)
    deduped: dict[tuple[int | None, str], Flashcard] = {}
    for card in all_cards:
        key = (card.chapter_num, card.term.lower())
        prev = deduped.get(key)
        if prev is None or len(card.definition) > len(prev.definition):
            deduped[key] = card
    all_cards = sorted(deduped.values(), key=lambda c: (c.chapter_num or 9999, c.term.lower()))

    elapsed = time.perf_counter() - started
    return ExtractResult(
        book=book,
        cards=all_cards,
        chapters_fetched=len(pages) - skipped,
        pages_skipped=skipped,
        pages_total=len(pages),
        elapsed_s=elapsed,
    )


# ---------------------------------------------------------------------------
# Attribution / licensing (OpenStax content is CC BY-NC-SA 4.0)
# ---------------------------------------------------------------------------

LICENSE_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/"


def book_free_url(book: BookMeta) -> str:
    """Canonical free-access page for a book, used for required attribution."""
    return f"https://openstax.org/details/books/{book.slug}"


def attribution_lines(book: BookMeta) -> list[str]:
    """Required CC BY-NC-SA 4.0 attribution + the affiliation disclaimer."""
    return [
        f'Content from OpenStax "{book.title}". Access for free at {book_free_url(book)}.',
        f"Licensed under CC BY-NC-SA 4.0 ({LICENSE_URL}). If you share these cards, "
        "you must credit OpenStax, keep them non-commercial, and license any "
        "derivative under the same terms.",
        "openstax-flash is not affiliated with, authored by, or endorsed by "
        "OpenStax or Rice University.",
    ]


def attribution_comment_block(book: BookMeta, prefix: str = "# ") -> str:
    """Attribution rendered as comment lines (ignored by Anki on import)."""
    return "".join(f"{prefix}{line}\n" for line in attribution_lines(book))


# ---------------------------------------------------------------------------
# Export formats
# ---------------------------------------------------------------------------


def card_back(card: Flashcard, fmt: str) -> str:
    if fmt in ("anki", "anki-latex", "tsv", "csv"):
        return card.definition_latex if fmt == "anki-latex" else card.definition
    if fmt == "md":
        return card.definition_latex if "$" in card.definition_latex else card.definition
    return card.definition


def export_json(result: ExtractResult, path: Path | None) -> str:
    payload = {
        "book": asdict(result.book),
        "meta": {
            "card_count": len(result.cards),
            "chapters_fetched": result.chapters_fetched,
            "pages_total": result.pages_total,
            "pages_skipped": result.pages_skipped,
            "elapsed_s": round(result.elapsed_s, 2),
            "source": "OpenStax",
            "source_url": book_free_url(result.book),
            "license": "CC BY-NC-SA 4.0",
            "license_url": LICENSE_URL,
            "attribution": " ".join(attribution_lines(result.book)),
        },
        "cards": [asdict(c) for c in result.cards],
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if path:
        path.write_text(text + "\n", encoding="utf-8")
    return text


def export_markdown(result: ExtractResult, path: Path | None) -> str:
    lines = [
        f"# {result.book.title} — Flashcards",
        "",
        f"**{len(result.cards)}** terms from OpenStax Key Terms / Glossary / Summary pages.",
        "",
        "> " + "  \n> ".join(attribution_lines(result.book)),
        "",
    ]
    current_ch: int | None = None
    for card in result.cards:
        if card.chapter_num != current_ch:
            current_ch = card.chapter_num
            lines.extend(["", f"## Chapter {card.chapter_num}: {card.chapter_title}", ""])
        back = card.definition_latex if "$" in card.definition_latex else card.definition
        lines.extend([f"### {card.term}", "", back, ""])
    text = "\n".join(lines).rstrip() + "\n"
    if path:
        path.write_text(text, encoding="utf-8")
    return text


def export_tsv(result: ExtractResult, path: Path | None, *, latex: bool = False, header: bool = False) -> str:
    rows: list[str] = []
    if header:
        rows.append("Front\tBack\tTags")
    fmt = "anki-latex" if latex else "anki"
    for card in result.cards:
        front = card.term.replace("\t", " ").replace("\n", " ")
        back = card_back(card, fmt).replace("\t", " ").replace("\n", " ")
        tags = " ".join(card.tags)
        rows.append(f"{front}\t{back}\t{tags}")
    text = attribution_comment_block(result.book) + "\n".join(rows) + ("\n" if rows else "")
    if path:
        path.write_text(text, encoding="utf-8")
    return text


def export_csv(result: ExtractResult, path: Path | None) -> str:
    buf: list[list[str]] = [
        ["term", "definition", "definition_latex", "chapter_num", "chapter_title", "page_slug", "tags"]
    ]
    for card in result.cards:
        buf.append(
            [
                card.term,
                card.definition,
                card.definition_latex,
                str(card.chapter_num or ""),
                card.chapter_title,
                card.page_slug,
                " ".join(card.tags),
            ]
        )
    header = attribution_comment_block(result.book)
    if path:
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write(header)
            csv.writer(f).writerows(buf)
        return path.read_text(encoding="utf-8")
    sio = io.StringIO()
    sio.write(header)
    csv.writer(sio).writerows(buf)
    return sio.getvalue()


def export_quizlet(result: ExtractResult, path: Path | None) -> str:
    """Quizlet CSV: term, definition (comma-separated, quoted)."""
    sio = io.StringIO()
    writer = csv.writer(sio, quoting=csv.QUOTE_ALL)
    for card in result.cards:
        writer.writerow([card.term, card.definition])
    text = sio.getvalue()
    if path:
        path.write_text(text, encoding="utf-8")
    return text


EXPORTERS = {
    "json": lambda r, p: export_json(r, p),
    "md": lambda r, p: export_markdown(r, p),
    "markdown": lambda r, p: export_markdown(r, p),
    "tsv": lambda r, p: export_tsv(r, p, latex=False, header=False),
    "csv": lambda r, p: export_csv(r, p),
    "anki": lambda r, p: export_tsv(r, p, latex=False, header=True),
    "anki-latex": lambda r, p: export_tsv(r, p, latex=True, header=True),
    "anki-tsv": lambda r, p: export_tsv(r, p, latex=False, header=True),
    "quizlet": lambda r, p: export_quizlet(r, p),
}


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def parse_chapter_list(raw: str | None) -> set[int] | None:
    if not raw:
        return None
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part and part[0].isdigit():
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def cmd_list(args: argparse.Namespace) -> int:
    books = load_books_index(args.cache_dir)
    if args.subject:
        subj = args.subject.lower()
        books = [b for b in books if any(subj in s.lower() for s in b.subjects)]
    if args.query:
        q = args.query.lower()
        books = [b for b in books if q in b.slug.lower() or q in b.title.lower()]

    for book in books:
        subs = ", ".join(book.subjects) if book.subjects else "—"
        rex = "✓" if book.rex_entry_url else "·"
        print(f"{rex} {book.slug:42} {book.title:38} [{subs}]")
    print(f"\n{len(books)} books", file=sys.stderr)
    return 0


def cmd_chapters(args: argparse.Namespace) -> int:
    book = resolve_book(args.slug, args.cache_dir)
    try:
        _, tree = load_book_tree(book, args.cache_dir)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    pages = prefer_term_pages(discover_term_pages(tree))
    if not pages:
        print(f"No term pages found for {book.slug}", file=sys.stderr)
        return 1
    print(f"{book.title} ({book.slug}) — {len(pages)} term pages\n")
    for p in pages:
        ch = f"ch{p.chapter_num:>2}" if p.chapter_num else "   "
        print(f"  {ch}  {p.kind:10}  {p.slug:30}  {strip_html(p.chapter_title)[:40]}")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    chapters = None if args.all else parse_chapter_list(args.chapters)
    result = extract_flashcards(
        args.slug,
        chapters=chapters,
        cache_dir=args.cache_dir,
        concurrency=args.concurrency,
        include_section_tables=args.section_tables,
        verbose=args.verbose,
    )

    out_path = Path(args.output) if args.output else None
    fmt = args.format.lower()
    if fmt not in EXPORTERS:
        raise SystemExit(f"Unknown format '{fmt}'. Choices: {', '.join(sorted(EXPORTERS))}")

    text = EXPORTERS[fmt](result, out_path)
    if out_path is None:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")

    dest = str(out_path) if out_path else "stdout"
    msg = (
        f"Extracted {len(result.cards)} cards from {result.chapters_fetched}/{result.pages_total} "
        f"pages in {result.elapsed_s:.1f}s → {dest}"
    )
    print(msg, file=sys.stderr)
    print(
        f"License: OpenStax content under CC BY-NC-SA 4.0 — {book_free_url(result.book)}\n"
        "         Attribution is embedded in the output. If you share these cards, keep "
        "them non-commercial and under the same license.",
        file=sys.stderr,
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    books = load_books_index(args.cache_dir)
    if args.query:
        q = args.query.lower()
        books = [b for b in books if q in b.slug.lower() or q in b.title.lower()]
    if args.slug:
        books = [resolve_book(args.slug, args.cache_dir)]

    ok, warn, fail = 0, 0, 0
    for book in books:
        try:
            _, tree = load_book_tree(book, args.cache_dir)
            pages = prefer_term_pages(discover_term_pages(tree))
            if not pages:
                print(f"WARN  {book.slug:42} 0 term pages")
                warn += 1
                continue

            if args.sample:
                sample_page = pick_sample_page(pages)
                url = f"https://openstax.org/books/{book.slug}/pages/{sample_page.slug}"
                page_html = http_get(url, args.cache_dir)
                pairs = parse_term_page(page_html, book.slug, args.cache_dir, False)
                n = len(pairs)
                status = "OK" if n > 0 else "WARN"
                print(f"{status:4}  {book.slug:42} {len(pages):3} pages  sample={n:3} terms  ({sample_page.slug})")
                if n > 0:
                    ok += 1
                else:
                    warn += 1
            else:
                print(f"OK    {book.slug:42} {len(pages):3} term pages")
                ok += 1
        except Exception as exc:
            print(f"FAIL  {book.slug:42} {exc}")
            fail += 1

    print(f"\nSummary: {ok} ok, {warn} warn, {fail} fail (of {len(books)})", file=sys.stderr)
    return 1 if fail else 0


def build_parser() -> argparse.ArgumentParser:
    epilog = textwrap.dedent(
        """
        COMMANDS
          list       List all OpenStax books (filter with --query / --subject)
          chapters   Show term-page chapters available for a book
          extract    Download terms and export flashcards
          verify     Check which books have extractable term pages

        EXTRACT — CHAPTER SELECTION
          Default: all chapters in the book.
          --chapters 3        single chapter
          --chapters 1,4,7    comma-separated list
          --chapters 1-5      inclusive range
          --all               explicit all-chapters (same as default)

        EXPORT FORMATS
          anki          Tab-separated with header (Front, Back, Tags) — recommended for Anki
          anki-latex    Same as anki but definitions use $LaTeX$ for math (enable MathJax in Anki)
          anki-tsv      Alias for anki
          tsv           Plain TSV without header row
          csv           Full CSV with metadata columns
          json          Structured JSON (term + plain + latex definitions)
          md            Markdown grouped by chapter
          quizlet       Quizlet-compatible CSV (term, definition)

        ANKI IMPORT
          1. openstax_flash.py extract <slug> --format anki -o deck.txt
          2. Anki → File → Import → select deck.txt
          3. Type: Fields separated by Tab
          4. Map: Field 1 → Front, Field 2 → Back, Field 3 → Tags
          5. For math-heavy books use --format anki-latex and a note type with MathJax

        EXAMPLES
          %(prog)s list --query physics
          %(prog)s chapters university-physics-volume-1
          %(prog)s extract university-physics-volume-1 --format anki -o up1.txt
          %(prog)s extract college-physics-2e --chapters 1-5 --format md -o ch1-5.md
          %(prog)s extract organic-chemistry --format anki-latex -o ochem.txt -v
          %(prog)s extract microbiology --format anki -o micro.txt
          %(prog)s verify --sample
          %(prog)s verify --query physics --sample
        """
    ).strip()

    parser = argparse.ArgumentParser(
        prog="openstax-flash",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Generate flashcards from OpenStax Key Terms, Glossary, and Summary pages. "
            "Supports all Rex books on openstax.org with math-aware export."
        ),
        epilog=epilog,
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE,
        help=f"HTTP cache directory (default: {DEFAULT_CACHE})",
    )
    parser.add_argument("--no-cache", action="store_true", help="Disable HTTP cache")

    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list", help="List available OpenStax books")
    list_p.add_argument("--query", "-q", help="Filter by slug or title substring")
    list_p.add_argument("--subject", "-s", help="Filter by subject (e.g. Science)")
    list_p.set_defaults(func=cmd_list)

    ch_p = sub.add_parser("chapters", help="List term pages (chapters) for a book")
    ch_p.add_argument("slug", help="Book slug")
    ch_p.set_defaults(func=cmd_chapters)

    ext_p = sub.add_parser("extract", help="Extract flashcards from a book")
    ext_p.add_argument("slug", help="Book slug, e.g. university-physics-volume-1")
    ext_p.add_argument(
        "--format",
        "-f",
        default="anki",
        help="Output format: anki, anki-latex, tsv, csv, json, md, quizlet (default: anki)",
    )
    ext_p.add_argument("--output", "-o", help="Output file (default: stdout)")
    ext_p.add_argument(
        "--chapters",
        "-c",
        help="Chapter filter: '3', '1,4,7', or range '1-5' (default: all chapters)",
    )
    ext_p.add_argument(
        "--all",
        action="store_true",
        help="Extract all chapters (default when --chapters is omitted)",
    )
    ext_p.add_argument("--concurrency", type=int, default=8, help="Parallel fetches (default: 8)")
    ext_p.add_argument(
        "--section-tables",
        action="store_true",
        help="Fallback: parse section key-term tables (terms only)",
    )
    ext_p.add_argument("--verbose", "-v", action="store_true")
    ext_p.set_defaults(func=cmd_extract)

    ver_p = sub.add_parser("verify", help="Verify book compatibility")
    ver_p.add_argument("slug", nargs="?", help="Optional single book slug")
    ver_p.add_argument("--query", "-q", help="Filter books to verify")
    ver_p.add_argument(
        "--sample",
        action="store_true",
        help="Also fetch one chapter page and count parsed terms",
    )
    ver_p.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.no_cache:
        args.cache_dir = None
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
