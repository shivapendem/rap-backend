# =============================================================
# file_preview.py — on-demand DOCX -> PDF conversion for inline "View"
#
# Google Docs Viewer (docs.google.com/viewer?url=...) was the previous
# approach for previewing non-PDF files (mainly .docx resumes): hand it a
# presigned Spaces URL and let Google's servers fetch + render it. That
# depends on Google being able to fetch and parse that specific URL,
# which isn't guaranteed — it can fail ("no preview available") for
# perfectly valid, reachable files, with no way for us to fix it from our
# side.
#
# This instead reuses the same LibreOffice-headless conversion already
# used for tailored resume generation (phase6.py's _convert_to_pdf) to
# convert a DOCX to a real PDF on demand, so "View" endpoints can return
# actual application/pdf bytes and rely on nothing but the browser's own
# built-in PDF viewer — the same one that already renders tailored
# resumes correctly.
# =============================================================

import hashlib
import logging
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# All cached preview PDFs live under this one Spaces prefix, keyed by a
# hash of the source DOCX's own bytes — not by the source file's own S3
# key/path. Several base-resume code paths *edit a DOCX in place* (same
# S3 key, new content) rather than uploading under a new key each time —
# a path-keyed cache would happily keep serving the old PDF forever after
# an edit. Hashing the actual bytes means any content change is
# automatically a cache miss; no explicit invalidation needed anywhere.
PDF_PREVIEW_CACHE_PREFIX = "preview-cache/"


def convert_docx_bytes_to_pdf_bytes(docx_bytes: bytes) -> Optional[bytes]:
    """
    Convert DOCX bytes to PDF bytes for inline preview.
    Returns None if conversion fails for any reason — callers should fall
    back to their prior behavior (presigned URL / raw docx bytes) rather
    than error out, since this is a preview nicety, not the only way to
    get at the file (Download still serves the original).
    """
    from phase6 import _convert_to_pdf

    try:
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = Path(tmp) / "preview_input.docx"
            pdf_path = Path(tmp) / "preview_input.pdf"
            docx_path.write_bytes(docx_bytes)
            ok = _convert_to_pdf(docx_path, pdf_path)
            if ok and pdf_path.exists() and pdf_path.stat().st_size > 0:
                return pdf_path.read_bytes()
            logger.warning(
                "DOCX->PDF preview conversion returned no usable output "
                "(ok=%s, exists=%s) — falling back to external viewer.",
                ok, pdf_path.exists(),
            )
            return None
    except Exception as exc:
        logger.warning("DOCX->PDF preview conversion raised: %s", exc)
        return None


def get_or_convert_pdf_preview(docx_bytes: bytes) -> Optional[bytes]:
    """
    Same contract as convert_docx_bytes_to_pdf_bytes (returns PDF bytes or
    None), but checks a Spaces-backed cache first and populates it after a
    successful live conversion.

    PERF: every "View" click used to re-run a full LibreOffice conversion
    from scratch, even for the exact same, unchanged file — real cost
    (seconds) on every single click, and the first conversion after any
    idle period pays LibreOffice's cold-start penalty on top of that
    (10-20s+ before the OS file cache warms up, vs 2-4s once warm),
    putting it uncomfortably close to (or past) the subprocess's own 30s
    timeout. That combination is exactly "first click times out /
    silently falls back, second click works but takes forever" — this
    function is the fix: convert once, cache the result, every later view
    of that exact content is a plain Spaces fetch with no LibreOffice
    involved at all.
    """
    from s3_service import download_file_from_s3, upload_file_to_s3
    import io

    digest = hashlib.sha256(docx_bytes).hexdigest()
    cache_key = f"{PDF_PREVIEW_CACHE_PREFIX}{digest}.pdf"

    try:
        cached_bytes, _ct = download_file_from_s3(cache_key)
        if cached_bytes:
            return cached_bytes
    except Exception as exc:
        logger.warning("PDF preview cache lookup failed for %s: %s", cache_key, exc)

    pdf_bytes = convert_docx_bytes_to_pdf_bytes(docx_bytes)
    if pdf_bytes:
        try:
            upload_file_to_s3(io.BytesIO(pdf_bytes), cache_key, "application/pdf")
        except Exception as exc:
            # Cache write failing is never a reason to fail the view —
            # the freshly-converted bytes are still good to serve now,
            # this request just won't benefit from the cache next time.
            logger.warning("PDF preview cache write failed for %s: %s", cache_key, exc)
    return pdf_bytes


def is_docx_like(filename_or_mimetype: str) -> bool:
    hint = (filename_or_mimetype or "").lower()
    return hint.endswith(".docx") or "wordprocessingml" in hint