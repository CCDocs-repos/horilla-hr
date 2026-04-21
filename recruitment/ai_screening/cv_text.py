"""Extract plain text from a candidate's uploaded resume.

Supports PDF (pdfplumber) and DOC/DOCX (python-docx). Any failure returns a
placeholder string — the LLM is told to evaluate on whatever information is
available, matching job-applicant-platform's extractTextFromCV behavior.
"""

import logging
import os
from typing import IO

import pdfplumber
import docx

logger = logging.getLogger(__name__)

MAX_CHARS = 8000


def _truncate(text: str) -> str:
    return text[:MAX_CHARS]


def _read_bytes(resume_file) -> bytes:
    if hasattr(resume_file, "read"):
        pos = resume_file.tell() if hasattr(resume_file, "tell") else None
        resume_file.seek(0) if hasattr(resume_file, "seek") else None
        data = resume_file.read()
        if pos is not None:
            try:
                resume_file.seek(pos)
            except Exception:
                pass
        return data
    with open(resume_file, "rb") as fh:
        return fh.read()


def _filename(resume_file) -> str:
    name = getattr(resume_file, "name", None) or str(resume_file)
    return os.path.basename(name)


def extract_cv_text(resume_file) -> str:
    name = _filename(resume_file)
    ext = os.path.splitext(name)[1].lower()
    try:
        data = _read_bytes(resume_file)
    except Exception:
        logger.exception("Failed to read resume bytes for %s", name)
        return "(Resume file could not be read. Evaluate based on other available information.)"

    if ext == ".pdf":
        try:
            import io as _io
            with pdfplumber.open(_io.BytesIO(data)) as pdf:
                parts = []
                for page in pdf.pages:
                    parts.append(page.extract_text() or "")
                text = "\n".join(p for p in parts if p).strip()
            if not text:
                return "(no text extracted from PDF)"
            return _truncate(text)
        except Exception:
            logger.exception("pdfplumber failed for %s", name)
            return "(PDF could not be parsed — the file may be corrupted, image-based, or password-protected. Evaluate based on other available information.)"

    if ext in (".doc", ".docx"):
        try:
            import io as _io
            document = docx.Document(_io.BytesIO(data))
            text = "\n".join(p.text for p in document.paragraphs if p.text)
            if not text:
                return "(no text extracted from document)"
            return _truncate(text)
        except Exception:
            logger.exception("python-docx failed for %s", name)
            return "(Document could not be parsed. Evaluate based on other available information.)"

    return f"(Unsupported file format: {ext}. Evaluate based on other available information.)"
