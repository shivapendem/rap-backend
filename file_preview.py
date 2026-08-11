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

import logging
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


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


def is_docx_like(filename_or_mimetype: str) -> bool:
    hint = (filename_or_mimetype or "").lower()
    return hint.endswith(".docx") or "wordprocessingml" in hint