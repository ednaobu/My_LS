"""Data Ingestion & Digitization: Smart OCR and the ingest pipeline.

`ingest()` is where the monolith shows its hand: OCR, extraction,
categorization, repository write, amendment linking and deadline scheduling
are plain function calls inside ONE database transaction. Either the contract
is fully processed or nothing is stored - no events, no eventual consistency.
"""
import base64
import hashlib
import io
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import categorization, extraction, governing, repository, tracking

OUR_COMPANY = os.environ.get("OUR_COMPANY", "Acme Corp")
SUPPORTED = {".txt", ".md", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}


class UnreadableDocument(ValueError):
    pass


def ocr(filename: str, data: bytes) -> tuple[str, float]:
    """Return machine-readable text and a confidence score."""
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        text = data.decode("utf-8", errors="replace")
    elif suffix == ".pdf":
        text = _pdf_text(data)
    elif suffix in SUPPORTED:
        text = _image_text(data)
    else:
        raise UnreadableDocument(f"unsupported file type {suffix!r}")
    return normalize(text)


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise UnreadableDocument("PDF support needs `pip install pypdf`") from exc
    text = "\n\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
    return text if text.strip() else _image_text(data)  # scanned PDF: no text layer


def _image_text(data: bytes) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise UnreadableDocument("scanned documents need `pip install pytesseract pillow` + tesseract") from exc
    return pytesseract.image_to_string(Image.open(io.BytesIO(data)))


def normalize(text: str) -> tuple[str, float]:
    """Clean OCR artefacts: hyphenated line breaks, stray whitespace, ligatures."""
    text = text.replace("ﬁ", "fi").replace("ﬂ", "fl").replace("\r\n", "\n")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    tokens = text.split()
    confidence = round(sum(bool(re.fullmatch(r"[\w$.,;:()'\"%/&-]+", t)) for t in tokens) / (len(tokens) or 1), 3)
    return text, confidence


def ingest(conn: sqlite3.Connection, *, text: str, title: str | None = None, filename: str | None = None,
           counterparty: str | None = None, department: str | None = None, source: str = "upload",
           source_ref: str | None = None, ocr_confidence: float = 1.0) -> tuple[dict, bool]:
    """Returns (contract, created). Re-ingesting identical text is a no-op."""
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    if existing := repository.find_by_hash(conn, content_hash):
        return repository.get(conn, existing), False

    values = extraction.extract(text)
    title = title or _title(text, filename)
    category = categorization.categorize(text, title)
    counterparty = counterparty or extraction.counterparty(values.get("parties", {}).get("value"), OUR_COMPANY)

    parent_id = governing.find_parent(conn, text, counterparty) if category["contract_type"] == "Amendment" else None
    department = department or category["department"]
    if parent_id and not department:
        department = repository.get(conn, parent_id)["department"]

    contract_id = repository.insert_contract(
        conn, title=title, counterparty=counterparty, contract_type=category["contract_type"],
        department=department or "Legal", type_confidence=category["confidence"], parent_id=parent_id,
        source=source, source_ref=source_ref, filename=filename, content_hash=content_hash,
        ocr_confidence=ocr_confidence, text=text, ingested_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    repository.save_values(conn, contract_id, values)
    tracking.reschedule(conn, parent_id or contract_id)  # an amendment can move its parent's deadlines
    return repository.get(conn, contract_id), True


def ingest_file(conn: sqlite3.Connection, filename: str, data: bytes, **kwargs) -> tuple[dict, bool]:
    text, confidence = ocr(filename, data)
    return ingest(conn, text=text, filename=filename, ocr_confidence=confidence, **kwargs)


def ingest_base64(conn: sqlite3.Connection, filename: str, content_base64: str, **kwargs) -> tuple[dict, bool]:
    return ingest_file(conn, filename, base64.b64decode(content_base64), **kwargs)


def sync_folder(conn: sqlite3.Connection, folder: str, provider: str = "local") -> list[dict]:
    """Cloud-storage sync stand-in: ingest every supported file in a folder (S3/Drive/Box in production)."""
    results = []
    for path in sorted(Path(folder).iterdir()):
        if path.suffix.lower() in SUPPORTED:
            contract, created = ingest_file(conn, path.name, path.read_bytes(), source=f"sync:{provider}", source_ref=str(path))
            results.append({"file": path.name, "contract_id": contract["id"], "created": created,
                            "contract_type": contract["contract_type"]})
    return results


def _title(text: str, filename: str | None) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    first = first.title() if first.isupper() else first
    return first[:120] if 3 < len(first) < 120 else (Path(filename).stem if filename else "Untitled agreement")
