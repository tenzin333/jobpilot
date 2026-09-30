from pathlib import Path
import docx2txt
from pypdf import PdfReader


def extract_text(file_path: str) -> str:
    """Extract text from PDF and DOCX documents."""

    target = Path(file_path)

    if not target.is_file():
        raise FileNotFoundError(
            f"File not found: {target}"
        )

    if target.suffix.lower() == ".docx":
        text = docx2txt.process(str(target))

    elif target.suffix.lower() == ".pdf":
        reader = PdfReader(target)

        text = "\n".join(
            page.extract_text() or ""
            for page in reader.pages
        )

    else:
        raise ValueError(
            f"Unsupported file format: {target.suffix}"
        )

    if not text.strip():
        raise ValueError(
            "No extractable text found in the document."
        )

    return text.strip()