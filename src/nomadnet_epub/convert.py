from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path

import ebooklib
from ebooklib import epub

from .catalog import (
    BookMeta,
    TocEntry,
    load_state,
    publish_epub_file,
    remove_book_outputs,
    save_state,
    write_book_index,
    write_root_index,
)
from .config import Settings, ensure_dirs
from .micron import (
    coalesce_chapter_headings,
    html_to_blocks,
    library_link,
    link,
    page_filename,
    page_path,
    resolve_against,
    resolve_epub_href,
    slugify,
    word_count,
)

log = logging.getLogger(__name__)


def list_epubs(epubs_dir: Path) -> list[Path]:
    if not epubs_dir.exists():
        return []
    return sorted(
        p for p in epubs_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".epub"
    )


def _meta_values(book: epub.EpubBook, name: str) -> list[str]:
    values = book.get_metadata("DC", name) or []
    out: list[str] = []
    for item in values:
        if isinstance(item, tuple) and item:
            out.append(str(item[0]))
        elif item:
            out.append(str(item))
    return out


def _book_title(book: epub.EpubBook, epub_path: Path) -> str:
    titles = _meta_values(book, "title")
    return titles[0].strip() if titles and titles[0].strip() else epub_path.stem


def _book_author(book: epub.EpubBook) -> str:
    creators = _meta_values(book, "creator")
    return ", ".join(c.strip() for c in creators if c.strip())


def _unique_slug(epub_path: Path, taken: set[str]) -> str:
    base = slugify(epub_path.stem) or "book"
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _norm(name: str) -> str:
    return resolve_epub_href(name)


def _item_by_href(book: epub.EpubBook, href: str) -> epub.EpubItem | None:
    href = _norm(href)
    items = list(book.get_items())
    for item in items:
        name = _norm(item.get_name())
        if name == href:
            return item
    for item in items:
        name = _norm(item.get_name())
        if name.endswith("/" + href) or name.endswith(href) or href.endswith(name):
            return item
    basename = href.split("/")[-1]
    for item in items:
        if _norm(item.get_name()).split("/")[-1] == basename:
            return item
    return None


def _match_doc_key(target: str, known: dict[str, int]) -> int | None:
    target = _norm(target)
    if target in known:
        return known[target]
    basename = target.split("/")[-1]
    for key, page in known.items():
        if key == target or key.endswith("/" + target) or target.endswith(key):
            return page
        if key.split("/")[-1] == basename:
            return page
    return None


def _spine_documents(book: epub.EpubBook) -> list[epub.EpubItem]:
    docs: list[epub.EpubItem] = []
    for spine_id, _linear in book.spine:
        item = book.get_item_with_id(spine_id)
        if item is not None and item.get_type() == ebooklib.ITEM_DOCUMENT:
            docs.append(item)
    if docs:
        return docs
    return [
        item
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)
        if "nav.xhtml" not in item.get_name().lower()
    ]


def _flatten_toc(entries, depth: int = 0) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for entry in entries or []:
        if isinstance(entry, epub.Link):
            if entry.href:
                out.append((depth, entry.title or entry.href, entry.href))
        elif isinstance(entry, (list, tuple)) and entry:
            first = entry[0]
            rest = entry[1] if len(entry) > 1 else None
            if isinstance(first, epub.Link):
                if first.href:
                    out.append((depth, first.title or first.href, first.href))
                if isinstance(rest, (list, tuple)):
                    out.extend(_flatten_toc(rest, depth + 1))
            elif isinstance(first, epub.Section):
                if isinstance(rest, (list, tuple)):
                    out.extend(_flatten_toc(rest, depth))
            elif isinstance(rest, (list, tuple)):
                out.extend(_flatten_toc(rest, depth + 1))
    return out


def _toc_titles(book: epub.EpubBook) -> dict[str, str]:
    titles: dict[str, str] = {}
    for _depth, toc_title, href in _flatten_toc(book.toc):
        if not href:
            continue
        target, _ = resolve_against("", href)
        if target:
            titles.setdefault(_norm(target), toc_title)
    return titles


def _extract_image(
    book: epub.EpubBook,
    src: str,
    slug: str,
    dest_dir: Path,
    base_doc: str,
) -> tuple[str, str] | None:
    target, _frag = resolve_against(base_doc, src)
    if not target:
        target = _norm(src)
    item = _item_by_href(book, target)
    if item is None:
        return None
    raw = item.get_content()
    if not raw:
        return None

    orig = Path(item.get_name()).name
    ext = Path(orig).suffix
    if not ext:
        ext = mimetypes.guess_extension(item.get_media_type() or "") or ".bin"
    safe = re.sub(r"[^\w.-]", "_", Path(orig).stem)[:40] or "img"
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{safe}{ext}"
    out = dest_dir / filename
    n = 1
    while out.exists():
        filename = f"{safe}-{n}{ext}"
        out = dest_dir / filename
        n += 1
    out.write_bytes(raw)
    return orig, f"/file/books/{slug}/{filename}"


def _chunk_tagged(
    tagged: list[tuple[str, str]], words_per_page: int
) -> tuple[list[list[str]], dict[str, int]]:
    """Chunk (doc_href, block) pairs; return pages and doc→first-page map.

    Each EPUB spine document starts on a fresh page (chapter page-break).
    Within a document, blocks are still split around words_per_page.
    """
    pages: list[list[str]] = []
    doc_to_page: dict[str, int] = {}
    current: list[str] = []
    count = 0
    current_doc: str | None = None

    def flush() -> None:
        nonlocal current, count
        if current:
            pages.append(current)
            current = []
            count = 0

    for doc_href, block in tagged:
        if current_doc is not None and doc_href != current_doc:
            flush()
        current_doc = doc_href

        wc = word_count(block)
        if current and count + wc > words_per_page and count >= max(1, words_per_page // 3):
            flush()
        if doc_href not in doc_to_page:
            doc_to_page[doc_href] = len(pages) + 1
        current.append(block)
        count += wc
        if count >= words_per_page * 2 and len(current) > 1:
            pages.append(current[:-1])
            current = [current[-1]]
            count = word_count(current[0])

    flush()
    if not pages:
        pages = [[]]
    return pages, doc_to_page


def _doc_blocks(
    book: epub.EpubBook,
    doc: epub.EpubItem,
    *,
    settings: Settings,
    slug: str,
    book_files: Path,
    toc_titles: dict[str, str],
    doc_to_page: dict[str, int] | None,
) -> tuple[str, list[str]]:
    href = _norm(doc.get_name())
    chapter_title = toc_titles.get(href)
    if not chapter_title:
        base = href.split("/")[-1]
        for key, title in toc_titles.items():
            if key.split("/")[-1] == base:
                chapter_title = title
                break

    html = doc.get_content()
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")

    image_resolver = None
    if settings.images == "files":

        def image_resolver(src: str, _doc: str = href) -> tuple[str, str] | None:
            return _extract_image(book, src, slug, book_files, _doc)

    link_resolver = None
    if doc_to_page is not None:

        def link_resolver(raw_href: str, _doc: str = href) -> str | None:
            target, _frag = resolve_against(_doc, raw_href)
            if not target:
                return None
            page = _match_doc_key(target, doc_to_page)
            if page is None:
                return None
            return page_path(slug, page)

    blocks = html_to_blocks(
        html,
        images=settings.images,
        image_resolver=image_resolver,
        link_resolver=link_resolver,
        base_doc=href,
    )
    blocks = coalesce_chapter_headings(blocks)
    if chapter_title and (not blocks or not blocks[0].startswith(">")):
        blocks = [f">>{chapter_title}", *blocks]
    return href, blocks


def convert_epub(epub_path: Path, settings: Settings, slug: str) -> BookMeta:
    book = epub.read_epub(str(epub_path), options={"ignore_ncx": False})
    title = _book_title(book, epub_path)
    author = _book_author(book)

    remove_book_outputs(settings, slug)
    book_pages = settings.books_pages_dir / slug
    book_pages.mkdir(parents=True, exist_ok=True)
    book_files = settings.books_files_dir / slug

    spine = _spine_documents(book)
    toc_titles = _toc_titles(book)

    # Pass 1: chunk without internal links to establish document → page map
    tagged: list[tuple[str, str]] = []
    for doc in spine:
        href, blocks = _doc_blocks(
            book,
            doc,
            settings=settings,
            slug=slug,
            book_files=book_files,
            toc_titles=toc_titles,
            doc_to_page=None,
        )
        for block in blocks:
            tagged.append((href, block))
    _pages, doc_to_page = _chunk_tagged(tagged, settings.words)
    for doc in spine:
        doc_to_page.setdefault(_norm(doc.get_name()), 1)

    # Pass 2: reconvert with working internal links; keep page map from pass 1
    # for href targets (stable), then re-chunk for output.
    tagged = []
    for doc in spine:
        href, blocks = _doc_blocks(
            book,
            doc,
            settings=settings,
            slug=slug,
            book_files=book_files,
            toc_titles=toc_titles,
            doc_to_page=doc_to_page,
        )
        for block in blocks:
            tagged.append((href, block))
    pages, final_doc_to_page = _chunk_tagged(tagged, settings.words)

    # If chunking shifted, rebuild links once more against final map
    if final_doc_to_page != doc_to_page:
        tagged = []
        for doc in spine:
            href, blocks = _doc_blocks(
                book,
                doc,
                settings=settings,
                slug=slug,
                book_files=book_files,
                toc_titles=toc_titles,
                doc_to_page=final_doc_to_page,
            )
            for block in blocks:
                tagged.append((href, block))
        pages, final_doc_to_page = _chunk_tagged(tagged, settings.words)

    page_titles: list[str] = []
    for i, page_blocks in enumerate(pages, start=1):
        preview = next((b for b in page_blocks if b.strip()), "")
        if preview.startswith(">"):
            page_titles.append(preview.lstrip(">").strip()[:60])
        else:
            page_titles.append(f"Page {i}")

        prev_s = link("← Prev", page_path(slug, i - 1)) if i > 1 else "Prev"
        next_s = link("Next →", page_path(slug, i + 1)) if i < len(pages) else "Next"
        header = [
            f">{title}",
            "",
            f"{link('Book index', f'/page/books/{slug}/index.mu')} · {library_link()}",
            "",
            f"{prev_s} · {i}/{len(pages)} · {next_s}",
            "",
            "-",
            "",
        ]
        (book_pages / page_filename(i)).write_text(
            "\n".join(header) + "\n\n".join(page_blocks) + "\n",
            encoding="utf-8",
        )

    toc_entries: list[TocEntry] = []
    for depth, toc_title, href in _flatten_toc(book.toc):
        if not href:
            toc_entries.append(TocEntry(title=toc_title, page=None, depth=depth))
            continue
        target, _ = resolve_against("", href)
        page = _match_doc_key(target or href, final_doc_to_page)
        toc_entries.append(TocEntry(title=toc_title, page=page, depth=depth))

    stat = epub_path.stat()
    epub_file = publish_epub_file(epub_path, settings, slug)
    meta = BookMeta(
        slug=slug,
        title=title,
        author=author,
        epub_name=epub_path.name,
        page_count=len(pages),
        mtime=stat.st_mtime,
        size=stat.st_size,
        epub_file=epub_file,
    )
    write_book_index(settings, meta, page_titles, toc_entries)
    log.info(
        "Converted %s -> %s (%d pages, %d toc)",
        epub_path.name,
        slug,
        meta.page_count,
        len(toc_entries),
    )
    return meta


def _needs_convert(epub_path: Path, prev: BookMeta | None) -> bool:
    if prev is None:
        return True
    stat = epub_path.stat()
    return prev.mtime != stat.st_mtime or prev.size != stat.st_size


def convert_all(settings: Settings, *, force: bool = False) -> list[BookMeta]:
    ensure_dirs(settings)
    previous_raw = load_state(settings.state_path)
    by_name = {v["epub_name"]: BookMeta(**v) for v in previous_raw.values()}
    by_slug = {k: BookMeta(**v) for k, v in previous_raw.items()}

    epubs = list_epubs(settings.epubs_dir)
    books: dict[str, BookMeta] = {}
    taken: set[str] = set()
    changed = False

    for path in epubs:
        prev = by_name.get(path.name)
        slug = prev.slug if prev and prev.slug not in taken else _unique_slug(path, taken)
        taken.add(slug)

        try:
            needs = force or _needs_convert(path, prev) or not (
                settings.books_pages_dir / slug / "index.mu"
            ).exists()
            if needs:
                meta = convert_epub(path, settings, slug)
                changed = True
            elif prev and prev.slug == slug:
                meta = prev
            else:
                meta = convert_epub(path, settings, slug)
                changed = True
            books[meta.slug] = meta
        except Exception:
            log.exception("Failed to convert %s", path.name)
            if prev and prev.slug == slug:
                books[prev.slug] = prev

    for slug in list(by_slug.keys()):
        if slug not in books:
            remove_book_outputs(settings, slug)
            changed = True
            log.info("Removed stale book %s", slug)

    write_root_index(settings, list(books.values()))
    save_state(settings.state_path, books)
    if changed:
        log.info("Catalog updated (%d books)", len(books))
    else:
        log.info("No EPUB changes (%d books)", len(books))
    return list(books.values())
