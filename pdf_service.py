"""Extract page text from PDF bytes without knowledge-base side effects."""

from typing import TypedDict

import pymupdf


class PDFPage(TypedDict):
    page: int
    text: str


def extract_pdf_text(pdf_bytes: bytes) -> list[PDFPage]:
    """Return one-based page numbers and extracted text for a PDF."""
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
            return [
                {"page": page_number, "text": page.get_text()}
                for page_number, page in enumerate(document, start=1)
            ]
    except (RuntimeError, OSError, ValueError) as error:
        # PyMuPDF's FileDataError and EmptyFileError inherit RuntimeError.
        raise ValueError("PDF无法解析，请检查文件是否损坏") from error
