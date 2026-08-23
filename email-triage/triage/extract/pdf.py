"""Витягування тексту з PDF-вкладень."""
from __future__ import annotations

import io

from pypdf import PdfReader

from ..models import Attachment, ExtractedDoc


def extract_pdf_text(
    attachment: Attachment, *, max_pages: int = 30, max_chars: int = 8000
) -> ExtractedDoc:
    """Читає текст із PDF. Ніколи не кидає виняток — помилка їде в поле `error`."""
    doc = ExtractedDoc(source=attachment.filename, kind="pdf", text="")
    if not attachment.data:
        doc.error = "порожнє вкладення"
        return doc
    try:
        reader = PdfReader(io.BytesIO(attachment.data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                doc.error = "PDF захищений паролем"
                return doc
        pages = reader.pages[:max_pages]
        if len(reader.pages) > max_pages:
            doc.truncated = True
        chunks: list[str] = []
        for page in pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(c.strip() for c in chunks if c.strip()).strip()
        if not text:
            doc.error = (
                "у PDF нема текстового шару — ймовірно це скан. "
                "Розпізнавання зображень у цій версії не підключене"
            )
            return doc
        if len(text) > max_chars:
            text = text[:max_chars]
            doc.truncated = True
        doc.text = text
    except Exception as exc:  # пошкоджений або не-PDF файл
        doc.error = f"не вдалося прочитати PDF: {type(exc).__name__}: {exc}"
    return doc
