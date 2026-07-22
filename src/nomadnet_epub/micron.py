from __future__ import annotations

import re
import warnings
from html import unescape
from posixpath import dirname, join, normpath
from typing import Callable, Iterable
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

_WORD_RE = re.compile(r"\S+")

# Common EPUB chapter-title container classes (Barnes & Noble, etc.)
_CHAPTER_TITLE_CLASSES = {
    "ct",
    "cst",
    "fmht",
    "chapter",
    "chapter-title",
    "chaptertitle",
    "chaptitle",
    "title",
}

LinkResolver = Callable[[str], str | None]


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


def slugify(value: str, fallback: str = "book") -> str:
    value = value.strip().lower()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[-\s]+", "-", value).strip("-")
    return value[:80] or fallback


def escape_micron_plain(text: str) -> str:
    """Normalize whitespace for block text (strips edges)."""
    text = unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def escape_micron_inline(text: str) -> str:
    """Normalize whitespace but keep a single leading/trailing space if present."""
    text = unescape(text)
    text = text.replace("\xa0", " ")
    if not text:
        return ""
    leading = text[0].isspace()
    trailing = text[-1].isspace()
    text = re.sub(r"[ \t\r\n]+", " ", text).strip()
    if leading:
        text = " " + text
    if trailing:
        text = text + " "
    return text


def link(label: str, path: str, *, underline: bool = True) -> str:
    """Micron link: `[label`url] — optional underline for visibility.

    NomadNet browser requires same-node paths as `:/page/...` (leading colon).
    MeshChatX also accepts `/page/...`; we emit the colon form for both.
    """
    label = escape_micron_plain(label).replace("`", "'").replace("]", "")
    if path.startswith("/") and not path.startswith("//"):
        path = f":{path}"
    core = f"`[{label}`{path}]"
    if underline:
        return f"`_{core}`_"
    return core


def library_link(label: str = "Library") -> str:
    return link(f"← {label}", "/page/index.mu")


def _heading_prefix(name: str) -> str:
    level = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}.get(name, 2)
    return ">" * min(level, 3)


def _local_name(tag: Tag) -> str:
    name = (tag.name or "").lower()
    if "}" in name:
        name = name.rsplit("}", 1)[-1]
    return name


def _class_set(tag: Tag) -> set[str]:
    raw = tag.get("class") or []
    if isinstance(raw, str):
        raw = raw.split()
    return {str(c).lower() for c in raw}


def _is_chapter_title_container(tag: Tag) -> bool:
    classes = _class_set(tag)
    if classes & _CHAPTER_TITLE_CLASSES:
        return True
    # e.g. ctBT-T, fmhT-ish
    return any(
        c.startswith("ct") or c.startswith("cst") or "chapter" in c or c.startswith("fmh")
        for c in classes
    )


def resolve_epub_href(href: str) -> str:
    """Strip fragments and normalize relative hrefs from EPUB HTML."""
    parsed = urlparse(href)
    path = unquote(parsed.path)
    return path.lstrip("/")


def resolve_against(base_doc: str, href: str) -> tuple[str | None, str | None]:
    """Resolve an EPUB href relative to the current document.

    Returns (normalized_doc_path, fragment) or (None, None) for external URLs.
    """
    href = (href or "").strip()
    if not href or href.startswith("#"):
        frag = unquote(href[1:]) if href.startswith("#") else None
        return (base_doc, frag or None)

    parsed = urlparse(href)
    if parsed.scheme in ("http", "https", "mailto", "lxmf"):
        return (None, None)
    if parsed.scheme and parsed.scheme not in ("", "file"):
        return (None, None)

    frag = unquote(parsed.fragment) if parsed.fragment else None
    path = unquote(parsed.path or "")
    if not path:
        return (base_doc, frag)

    if path.startswith("/"):
        target = path.lstrip("/")
    else:
        target = normpath(join(dirname(base_doc), path))
    while target.startswith("../"):
        target = target[3:]
    return (target, frag)


def html_to_blocks(
    html: str,
    *,
    images: str = "none",
    image_resolver=None,
    link_resolver: LinkResolver | None = None,
    base_doc: str = "",
) -> list[str]:
    """Convert an HTML fragment into Micron paragraph/heading blocks."""
    soup = BeautifulSoup(html, "lxml-xml")
    if soup.find() is None:
        soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    body = soup.body if soup.body else soup
    blocks: list[str] = []

    def emit(block: str) -> None:
        block = block.strip()
        if block:
            blocks.append(block)

    def inline(node) -> str:
        if isinstance(node, NavigableString):
            return escape_micron_inline(str(node))
        if not isinstance(node, Tag):
            return ""
        name = _local_name(node)
        if name in ("br",):
            return "\n"
        if name in ("b", "strong", "i", "em"):
            # Keep prose plain — EPUB emphasis (drop caps, bio names, etc.)
            # turns into noisy Micron markers in a terminal reader.
            return "".join(inline(c) for c in node.children)
        if name == "a":
            href = (node.get("href") or "").strip()
            label = "".join(inline(c) for c in node.children).strip()
            # Anchor-only markers (<a id="c01"/>) — skip entirely
            if not href:
                return ""
            if not label:
                return ""
            if href.startswith(("lxmf://", "http://", "https://")):
                return link(label, href)
            if href.startswith("/") and not href.startswith("//"):
                return link(label, href)
            if link_resolver:
                resolved = link_resolver(href)
                if resolved:
                    return link(label, resolved)
            return label
        if name == "img":
            alt = escape_micron_plain(node.get("alt") or "image")
            src = (node.get("src") or "").strip()
            if images == "files" and image_resolver and src:
                resolved = image_resolver(src)
                if resolved:
                    return link(alt or resolved[0], resolved[1])
            return f"_[{alt} omitted]_"
        if name in ("span", "font", "u", "s", "sub", "sup", "code", "tt"):
            return "".join(inline(c) for c in node.children)
        return "".join(inline(c) for c in node.children)

    def walk(node) -> None:
        if isinstance(node, NavigableString):
            text = escape_micron_plain(str(node))
            if text:
                emit(text)
            return
        if not isinstance(node, Tag):
            return
        name = _local_name(node)
        if name in ("script", "style", "noscript", "head", "meta", "link"):
            return
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = "".join(inline(c) for c in node.children).strip()
            # Strip accidental emphasis wrappers around whole heading
            text = re.sub(r"^\*([^*]+)\*$", r"\1", text)
            text = re.sub(r"^_([^_]+)_$", r"\1", text)
            if text:
                emit(f"{_heading_prefix(name)}{text}")
            return

        if name in ("p", "div", "section", "article", "blockquote", "li"):
            # Chapter title containers → micron headings (no bold noise)
            if name == "div" and _is_chapter_title_container(node):
                text = "".join(inline(c) for c in node.children).strip()
                text = re.sub(r"^\*([^*]+)\*$", r"\1", text)
                text = re.sub(r"^_([^_]+)_$", r"\1", text)
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    # Pure chapter number → keep with following title if possible;
                    # emit as >>> for number-only, >> for titled
                    if text.isdigit() or (len(text) <= 3 and text.replace(".", "").isdigit()):
                        emit(f">>>{text}")
                    else:
                        emit(f">>{text}")
                return

            text = "".join(inline(c) for c in node.children)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if text:
                if name == "li":
                    emit(f"- {text}")
                elif name == "blockquote":
                    for line in text.splitlines() or [text]:
                        emit(f"  {line}" if line.strip() else "")
                else:
                    emit(text)
            return
        if name in ("ul", "ol", "table", "tbody", "thead", "tr", "td", "th", "body", "html", "nav"):
            for child in node.children:
                walk(child)
            return
        if name == "hr":
            emit("-")
            return
        if name == "img":
            emit(inline(node))
            return
        for child in node.children:
            walk(child)

    for child in list(body.children):
        walk(child)

    return [b for b in blocks if b.strip()]


def chunk_blocks(blocks: Iterable[str], words_per_page: int) -> list[list[str]]:
    """Split Micron blocks into pages of roughly words_per_page words."""
    pages: list[list[str]] = []
    current: list[str] = []
    count = 0

    for block in blocks:
        wc = word_count(block)
        if current and count + wc > words_per_page and count >= max(1, words_per_page // 3):
            pages.append(current)
            current = []
            count = 0
        current.append(block)
        count += wc
        if count >= words_per_page * 2 and len(current) > 1:
            pages.append(current[:-1])
            current = [current[-1]]
            count = word_count(current[0])

    if current:
        pages.append(current)
    return pages or [[]]


def page_filename(index: int) -> str:
    return f"p{index:03d}.mu"


def page_path(slug: str, index: int) -> str:
    return f"/page/books/{slug}/{page_filename(index)}"


def coalesce_chapter_headings(blocks: list[str]) -> list[str]:
    """Merge consecutive `>>>1` + `>>Title` into `>>1 - Title`."""
    out: list[str] = []
    i = 0
    while i < len(blocks):
        cur = blocks[i]
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if (
            cur.startswith(">>>")
            and not cur.startswith(">>>>")
            and nxt
            and nxt.startswith(">>")
            and not nxt.startswith(">>>")
        ):
            num = cur.lstrip(">").strip()
            title = nxt.lstrip(">").strip()
            if num and title:
                out.append(f">>{num} - {title}")
                i += 2
                continue
        out.append(cur)
        i += 1
    return out
