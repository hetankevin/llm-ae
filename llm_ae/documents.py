"""Bounded extraction of text and PDF manuscripts; no execution or URL fetching."""

import hashlib
import io
from pathlib import Path

from .config import AEError


MAX_DOCUMENT_BYTES = 25_000_000


def extract(data: bytes, filename: str):
    if len(data) > MAX_DOCUMENT_BYTES:
        raise AEError("Manuscript exceeds the 25 MB limit")
    suffix = Path(filename).suffix.lower()
    warnings = []
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise AEError('PDF support is missing. Install with: pip install -e ".[pdf]"') from exc
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise AEError("Encrypted PDF: provide an unlocked manuscript")
            if len(reader.pages) > 300:
                raise AEError("PDF exceeds the 300-page extraction limit")
            pages = []
            for i, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                if len(text.strip()) < 30:
                    warnings.append(f"Page {i} has little extractable text; inspect it manually")
                pages.append(f"[Page {i}]\n{text}")
            text = "\n\n".join(pages)
        except AEError:
            raise
        except Exception as exc:
            raise AEError(f"Could not extract PDF ({type(exc).__name__})") from exc
        warnings.append("PDF text extraction can lose equations, tables, and figures")
    elif suffix in {".txt", ".md", ".tex"}:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AEError("Text manuscripts must use UTF-8") from exc
    else:
        raise AEError("Supported manuscript formats: .pdf, .txt, .md, .tex")
    if len(text.strip()) < 80:
        raise AEError("Too little manuscript text to assess; supply text or an OCR copy")
    return text, warnings


def persist(home, identifier, data, filename):
    suffix = Path(filename).suffix.lower()
    path = home / "documents" / f"{identifier}{suffix}"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
    path.chmod(0o600)
    return str(path), hashlib.sha256(data).hexdigest()


def read_file(path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise AEError("Manuscript exceeds the 25 MB limit")
    return data

