import asyncio
import importlib.metadata
from dataclasses import dataclass

import pymupdf
import trafilatura
from trafilatura.metadata import extract_metadata

from .models import Passage, ReaderError, Source
from .storage import digest, now


@dataclass
class Extracted:
    title: str
    text: str
    published_at: str | None
    # page number and half-open offsets into the conserved extracted text
    pages: list[tuple[int | None, int, int]]
    extractor: str
    warnings: list[str]


def extract(body: bytes, media_type: str, settings) -> Extracted:
    warnings = []
    if media_type == "application/pdf":
        with pymupdf.open(stream=body, filetype="pdf") as doc:
            if doc.is_encrypted:
                raise ReaderError("Encrypted PDF is unsupported")
            if len(doc) > settings.max_pdf_pages:
                warnings.append("PDF truncated at configured page limit")
            parts, pages, offset = [], [], 0
            for i in range(min(len(doc), settings.max_pdf_pages)):
                text = doc[i].get_text(sort=True)
                available = settings.max_text_chars - offset
                if available <= 0:
                    warnings.append("PDF truncated at text limit")
                    break
                if len(text) > available:
                    text = text[:available]
                    warnings.append("PDF truncated at text limit")
                parts.append(text)
                pages.append((i + 1, offset, offset + len(text)))
                offset += len(text) + 1
            full_text = "\n".join(parts)
            title = str((doc.metadata or {}).get("title") or "PDF document")
            extractor = "pymupdf/" + importlib.metadata.version("pymupdf")
            published = None
    elif media_type in ("text/plain", "text/markdown"):
        full_text = body.decode("utf-8", errors="replace")
        title, published = "Text document", None
        pages = [(None, 0, len(full_text))]
        extractor = "utf8/1"
    else:
        full_text = trafilatura.extract(
            body, include_tables=True, include_comments=False, include_links=False,
            output_format="txt", favor_recall=True) or ""
        metadata = extract_metadata(body)
        title = (metadata.title if metadata else None) or "HTML document"
        published = str(metadata.date) if metadata and metadata.date else None
        pages = [(None, 0, len(full_text))]
        extractor = "trafilatura/" + importlib.metadata.version("trafilatura")
    if not full_text.strip():
        raise ReaderError("No selectable text; dynamic pages and OCR are not implemented")
    if len(full_text) > settings.max_text_chars:
        full_text = full_text[:settings.max_text_chars]
        pages = [(p, start, min(end, len(full_text))) for p, start, end in pages if start < len(full_text)]
        warnings.append("Document truncated at text limit")
    return Extracted(title, full_text, published, pages, extractor, warnings)


def make_source(original_url, final_url, body, media_type, extracted: Extracted, store):
    body_hash = store.write_object(body)
    text_hash = store.write_object(extracted.text.encode())
    source_id = "s_" + digest(final_url + ":" + body_hash)[:24]
    passages = []
    for page, first, last in extracted.pages:
        start = first
        while start < last:
            end = min(start + 2400, last)
            # Prefer a boundary, while retaining exact offsets.
            if end < last:
                boundary = extracted.text.rfind("\n", start + 1200, end)
                if boundary > start:
                    end = boundary
            value = extracted.text[start:end]
            if value.strip():
                passage_id = "p_" + digest(source_id + f":{start}:{end}")[:24]
                passages.append(Passage(passage_id=passage_id, source_id=source_id,
                                        text=value, start=start, end=end, page=page))
            if end >= last:
                break
            start = max(start + 1, end - 320)
    return Source(
        source_id=source_id, original_url=original_url, final_url=final_url,
        title=extracted.title, captured_at=now(), published_at=extracted.published_at,
        body_sha256=body_hash, text_sha256=text_hash, media_type=media_type,
        extractor=extracted.extractor, family_id="exact_" + body_hash, passages=passages)


async def read(fetcher, store, url, settings):
    async with asyncio.timeout(settings.http_timeout_seconds):
        final_url, media_type, body = await fetcher.fetch(url)
    # Parsing is bounded by byte/page limits and does not block HTTP cancellation.
    extracted = await asyncio.to_thread(extract, body, media_type, settings)
    source = make_source(url, final_url, body, media_type, extracted, store)
    return source, extracted.warnings
