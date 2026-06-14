"""Document loading: the escalation ladder.

  .txt/.json/.csv/.xml ─────────────────────────► text
  .pdf ──► text-layer probe ──► quality gate ──► text (escalatable)
                                    │ fail
  .png/.jpg/.webp ────────────────┴──────────► page images (vision)

A PDF that passes the gate can still be escalated to the vision path later if
the extractor reports it illegible.
"""

import base64
from dataclasses import dataclass, field
from pathlib import Path

import fitz

from . import config

TEXT_SUFFIXES = {".txt", ".json", ".csv", ".xml", ".md", ".eml"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


class UnsupportedFormat(Exception):
    pass


@dataclass
class SourceDoc:
    kind: str                      # 'text' | 'image'
    origin: str
    text: str | None = None
    images_b64: list[str] = field(default_factory=list)
    image_mime: str = "image/png"
    can_escalate: bool = False     # text-kind PDFs can be re-read visually
    route_note: str = ""           # how/why this path was chosen (traced)


def text_quality_ok(text: str, pages: int) -> tuple[bool, str]:
    stripped = text.strip()
    if len(stripped) < config.MIN_TEXT_CHARS_PER_PAGE * pages:
        return False, f"text layer too thin ({len(stripped)} chars over {pages} page(s))"
    printable = sum(1 for c in stripped if c.isprintable() or c.isspace())
    ratio = printable / len(stripped)
    if ratio < config.MIN_PRINTABLE_RATIO:
        return False, f"text layer looks like garbage (printable ratio {ratio:.2f})"
    if not any(c.isdigit() for c in stripped):
        return False, "text layer contains no digits — implausible for an invoice"
    return True, f"text layer ok ({len(stripped)} chars, printable ratio {ratio:.2f})"


def render_pdf_pages(path: Path) -> list[str]:
    doc = fitz.open(path)
    pages = []
    for page in doc:
        pix = page.get_pixmap(dpi=config.RENDER_DPI)
        pages.append(base64.b64encode(pix.tobytes("png")).decode())
    return pages


def load_document(path: Path) -> SourceDoc:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return SourceDoc(
            kind="text",
            origin=str(path),
            text=path.read_text(errors="replace"),
            route_note=f"plain-text format ({suffix})",
        )
    if suffix == ".pdf":
        doc = fitz.open(path)
        text = "\n".join(page.get_text() for page in doc)
        ok, why = text_quality_ok(text, len(doc))
        if ok:
            return SourceDoc(
                kind="text", origin=str(path), text=text, can_escalate=True,
                route_note=f"PDF text layer passed quality gate: {why}",
            )
        return SourceDoc(
            kind="image", origin=str(path), images_b64=render_pdf_pages(path),
            route_note=f"PDF escalated to vision: {why}",
        )
    if suffix in IMAGE_SUFFIXES:
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(suffix[1:], f"image/{suffix[1:]}")
        return SourceDoc(
            kind="image", origin=str(path),
            images_b64=[base64.b64encode(path.read_bytes()).decode()],
            image_mime=mime,
            route_note=f"image format ({suffix}), vision path",
        )
    raise UnsupportedFormat(f"unsupported document format: {suffix} ({path.name})")


def escalate_to_vision(doc: SourceDoc) -> SourceDoc:
    assert doc.can_escalate, "escalate_to_vision called on a non-escalatable document"
    path = Path(doc.origin)
    return SourceDoc(
        kind="image", origin=doc.origin, images_b64=render_pdf_pages(path),
        route_note="escalated to vision: extractor reported the text layer unusable",
    )


def image_content_parts(doc: SourceDoc) -> list[dict]:
    return [
        {"type": "image_url", "image_url": {"url": f"data:{doc.image_mime};base64,{b64}"}}
        for b64 in doc.images_b64
    ]
