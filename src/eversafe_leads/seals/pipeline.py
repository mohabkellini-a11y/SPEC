"""Seal extraction cascade + persistence (Module 4).

Per page, try the sources in order of trustworthiness and stop at the first that
yields a seal (SPEC §6 confidences): digital signature 0.98 → PDF text 0.85 →
OCR 0.60 (scaled by OCR confidence). Extracted PE licenses are validated against
the FBPE ``person`` registry from Module 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog
from sqlmodel import Session, select

from ..models import ExtractionMethod, Person, Seal
from . import digital_sig, ocr, pdf_text
from .extract import SealCandidate, extract_seal

log = structlog.get_logger()

CONF_DIGITAL = 0.98
CONF_TEXT = 0.85
CONF_OCR_BASE = 0.60


@dataclass
class SealExtraction:
    page_number: int
    method: ExtractionMethod
    confidence: float
    candidate: SealCandidate


def extract_from_pdf(pdf_path: Path | str, want_ocr: bool = True) -> list[SealExtraction]:
    pdf_path = Path(pdf_path)
    results: list[SealExtraction] = []

    # 1) Digital signature (whole document; strongest).
    for name in digital_sig.signer_names(pdf_path):
        cand = extract_seal(name)
        cand.engineer_name = cand.engineer_name or name.strip()
        results.append(SealExtraction(0, ExtractionMethod.digital_signature, CONF_DIGITAL, cand))
    if results:
        return results

    # 2) PDF text layer, per page.
    try:
        texts = pdf_text.page_texts(pdf_path)
    except ImportError:
        texts = []
    for i, text in enumerate(texts):
        cand = extract_seal(text)
        if not cand.is_empty:
            results.append(SealExtraction(i, ExtractionMethod.pdf_text, CONF_TEXT, cand))
    if results:
        return results

    # 3) OCR fallback (first page's title block).
    if want_ocr:
        res = ocr.ocr_titleblock(pdf_path, 0)
        if res and res.text:
            cand = extract_seal(res.text)
            if not cand.is_empty:
                conf = round(CONF_OCR_BASE * max(0.5, min(1.0, res.confidence)), 3)
                results.append(SealExtraction(0, ExtractionMethod.ocr, conf, cand))
    return results


def _validate_license(session: Session, license_number: str | None) -> Person | None:
    if not license_number:
        return None
    return session.exec(select(Person).where(Person.license_number == license_number)).first()


def persist_seals(
    session: Session, document_id: int, extractions: list[SealExtraction]
) -> list[Seal]:
    """Write Seal rows for a document, validating each license against FBPE."""
    written: list[Seal] = []
    for ex in extractions:
        c = ex.candidate
        person = _validate_license(session, c.license_number)
        discipline = c.discipline or (person.discipline if person else None)
        raw = c.raw_text
        if person:
            raw = f"[FBPE-validated: {person.name}] {raw}"
        seal = Seal(
            document_id=document_id,
            page_number=ex.page_number,
            engineer_name=c.engineer_name or (person.name if person else None),
            license_number=c.license_number,
            discipline=discipline,
            firm_name=None,
            extraction_method=ex.method,
            confidence=ex.confidence,
            raw_text=raw,
        )
        session.add(seal)
        written.append(seal)
    if written:
        session.commit()
        for s in written:
            session.refresh(s)
    log.info("seals.persisted", document_id=document_id, count=len(written))
    return written
