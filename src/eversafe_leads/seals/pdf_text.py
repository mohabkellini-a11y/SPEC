"""PDF text-layer extraction (Module 4, step 3).

Seals live in the drawing's title block — lower-right of the sheet. We read that
region first (SPEC §6: "lower-right quadrant"), then fall back to full-page text
so a CA line that sits just outside the crop is still captured.
"""

from __future__ import annotations

from pathlib import Path


def page_texts(
    pdf_path: Path | str, right_frac: float = 0.5, bottom_frac: float = 0.55
) -> list[str]:
    """Return per-page text, each page's lower-right quadrant first then its full
    text. Requires ``pdfplumber`` (the ``pdf`` extra)."""
    import pdfplumber  # local import: optional dependency

    out: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            crop = page.crop(
                (page.width * right_frac, page.height * bottom_frac, page.width, page.height)
            )
            quad = crop.extract_text() or ""
            full = page.extract_text() or ""
            out.append((quad + "\n" + full).strip())
    return out
