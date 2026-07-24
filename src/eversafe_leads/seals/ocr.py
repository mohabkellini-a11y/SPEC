"""OCR fallback (Module 4, step 4).

Last resort when a sheet is a flat scan with no text layer. SPEC §6: rasterize at
300 DPI, crop the right 25% / bottom 30% (the title block), try rotations
0/90/180/270, keep the highest mean OCR confidence. Heavy, optional deps
(``pymupdf`` + ``pytesseract``) — guarded so the package imports without them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DPI = 300
ROTATIONS = (0, 90, 180, 270)


@dataclass
class OcrResult:
    text: str
    confidence: float  # 0..1 mean OCR confidence


def available() -> bool:
    try:
        import fitz  # noqa: F401  (pymupdf)
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return True


def ocr_titleblock(pdf_path: Path | str, page_number: int = 0) -> OcrResult | None:
    """OCR the title-block region of one page. Returns ``None`` if OCR deps are
    unavailable or nothing legible was found."""
    if not available():
        return None
    import fitz
    import pytesseract
    from PIL import Image

    doc = fitz.open(str(pdf_path))
    page = doc[page_number]
    zoom = DPI / 72
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    # crop right 25% / bottom 30%
    crop = img.crop((int(img.width * 0.75), int(img.height * 0.70), img.width, img.height))

    best: OcrResult | None = None
    for rot in ROTATIONS:
        rotated = crop.rotate(rot, expand=True)
        data = pytesseract.image_to_data(rotated, output_type=pytesseract.Output.DICT)
        confs = [
            int(c) for c in data.get("conf", []) if str(c).lstrip("-").isdigit() and int(c) >= 0
        ]
        if not confs:
            continue
        mean_conf = sum(confs) / len(confs) / 100.0
        text = " ".join(w for w in data.get("text", []) if w.strip())
        if best is None or mean_conf > best.confidence:
            best = OcrResult(text=text, confidence=mean_conf)
    return best
