from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Settings
from .micron import library_link, link, page_path


@dataclass
class BookMeta:
    slug: str
    title: str
    author: str
    epub_name: str
    page_count: int
    mtime: float
    size: int
    epub_file: str | None = None  # NomadNet /file/... path when published


@dataclass
class TocEntry:
    title: str
    page: int | None
    depth: int = 0


def load_state(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("books", {})
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(path: Path, books: dict[str, BookMeta]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"books": {k: asdict(v) for k, v in books.items()}}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def safe_epub_filename(epub_name: str, slug: str) -> str:
    name = Path(epub_name).name
    if not name.lower().endswith(".epub"):
        name = f"{name}.epub"
    stem = re.sub(r"[^\w.\-]+", "-", Path(name).stem).strip("-._") or slug
    stem = re.sub(r"-{2,}", "-", stem)[:80].strip("-") or slug
    return f"{stem}.epub"


def publish_epub_file(epub_path: Path, settings: Settings, slug: str) -> str:
    """Copy EPUB into NomadNet files storage; return /file/... URL path."""
    dest_dir = settings.books_files_dir / slug
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_epub_filename(epub_path.name, slug)
    dest = dest_dir / filename
    shutil.copy2(epub_path, dest)
    return f"/file/books/{slug}/{filename}"


def write_root_index(settings: Settings, books: list[BookMeta]) -> Path:
    lines = [
        f">{settings.index_title}",
        "",
        settings.description,
        "",
        ">>Books",
        "",
    ]
    if not books:
        lines.append("No books yet. Drop `.epub` files into the epubs folder.")
    else:
        for book in sorted(books, key=lambda b: b.title.lower()):
            author = f" — {book.author}" if book.author else ""
            pages = f" ({book.page_count} pages)" if book.page_count else ""
            lines.append(
                f"{link(book.title, f'/page/books/{book.slug}/index.mu')}{author}{pages}"
            )
            lines.append("")

    out = settings.pages_dir / "index.mu"
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out


def write_book_index(
    settings: Settings,
    meta: BookMeta,
    page_titles: list[str],
    toc_entries: list[TocEntry] | None = None,
) -> Path:
    book_dir = settings.books_pages_dir / meta.slug
    book_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f">{meta.title}",
        "",
    ]
    if meta.author:
        lines.append(f"By {meta.author}")
        lines.append("")
    lines.append(library_link("Library"))
    lines.append("")

    usable_toc = [e for e in (toc_entries or []) if e.page]
    if usable_toc:
        lines.append(">>Contents")
        lines.append("")
        for entry in usable_toc:
            indent = "  " * entry.depth
            lines.append(f"{indent}{link(entry.title, page_path(meta.slug, entry.page))}")
        lines.append("")
        lines.append(f"{link('Start reading →', page_path(meta.slug, 1))}")
        lines.append("")
    else:
        lines.append(">>Pages")
        lines.append("")
        for i, title in enumerate(page_titles, start=1):
            label = title or f"Page {i}"
            lines.append(link(label, page_path(meta.slug, i)))
        lines.append("")

    if meta.epub_file:
        lines.append(">>Download")
        lines.append("")
        size = format_bytes(meta.size) if meta.size else ""
        label = f"EPUB ({size})" if size else "EPUB"
        lines.append(link(label, meta.epub_file))
        lines.append("")

    out = book_dir / "index.mu"
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out


def remove_book_outputs(settings: Settings, slug: str) -> None:
    pages = settings.books_pages_dir / slug
    files = settings.books_files_dir / slug
    if pages.exists():
        shutil.rmtree(pages)
    if files.exists():
        shutil.rmtree(files)
