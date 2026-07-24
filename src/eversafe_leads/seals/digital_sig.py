"""Digital-signature extraction (Module 4, step 2).

Florida rules (F.A.C. 61G15-23) let engineers seal electronically with a digital
signature; when present it is the most trustworthy source of the sealing
engineer's identity (confidence 0.98). We read the signer's name from the PDF's
signature dictionary via ``pikepdf`` (the ``pdf`` extra). No signature → empty.
"""

from __future__ import annotations

from pathlib import Path


def signer_names(pdf_path: Path | str) -> list[str]:
    """Return the human-readable signer names on the PDF's signature fields.

    Best-effort and dependency-guarded: returns ``[]`` if ``pikepdf`` is absent
    or the PDF has no AcroForm signature fields.
    """
    try:
        import pikepdf  # local import: optional dependency
    except ImportError:
        return []

    names: list[str] = []
    try:
        with pikepdf.open(str(pdf_path)) as pdf:
            root = pdf.Root
            if "/AcroForm" not in root:
                return []
            fields = root.AcroForm.get("/Fields", [])
            for f in fields:
                ft = str(f.get("/FT", ""))
                if ft != "/Sig":
                    continue
                sig = f.get("/V")
                if sig is None:
                    continue
                name = sig.get("/Name")
                if name is not None:
                    names.append(str(name))
    except Exception:  # noqa: BLE001 - a malformed PDF must not crash the run
        return names
    return names
