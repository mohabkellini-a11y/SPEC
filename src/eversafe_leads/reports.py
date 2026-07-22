"""Outputs (SPEC §8): daily digest and CSV export.

The digest leads with HOT fire-review signals — reviewer comment quoted and a
suggested opening line — then the highest-scoring new permits. The CSV is a flat
file for CRM import.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path

from sqlmodel import Session, select

from .models import Party, Permit, Signal
from .review_monitor import ReviewMonitor
from .scoring import ScoredLead, Scorer

DIGEST_DIR = Path("data/digests")


def _contractor_for(session: Session, permit_id: int) -> Party | None:
    return session.exec(
        select(Party).where(Party.permit_id == permit_id, Party.role == "contractor")
    ).first()


def _opening_line(permit: Permit, signal: Signal) -> str:
    who = "your team"
    addr = permit.address_line1 or "the project"
    if signal.kind == "fire_review_rejection":
        return (
            f"Saw {permit.record_number} at {addr} came back with fire-review "
            f"corrections — we stamp and turn around sprinkler/alarm packages fast "
            f"if {who} needs a PE to clear it."
        )
    if signal.kind == "second_fire_cycle":
        return (
            f"Noticed {permit.record_number} at {addr} is on a second fire-review "
            f"cycle. Happy to take a look at the comments and get it approved."
        )
    return (
        f"{permit.record_number} at {addr} has been sitting in fire review a while — "
        f"we can help move it."
    )


def build_digest(
    session: Session,
    scorer: Scorer | None = None,
    monitor: ReviewMonitor | None = None,
    today: date | None = None,
    top_n: int = 25,
) -> str:
    today = today or date.today()
    scorer = scorer or Scorer()

    permits = session.exec(select(Permit)).all()
    signals_by_permit: dict[int, list[Signal]] = {}
    for s in session.exec(select(Signal)).all():
        signals_by_permit.setdefault(s.permit_id, []).append(s)

    scored: list[ScoredLead] = []
    for p in permits:
        scored.append(scorer.score_permit(p, signals=signals_by_permit.get(p.id, []), today=today))
    scored.sort(key=lambda sl: sl.score, reverse=True)

    lines: list[str] = [f"# EverSafe daily brief — {today.isoformat()}", ""]

    # 1) HOT fire-review signals first.
    hot = [s for s in session.exec(select(Signal)).all() if s.kind == "fire_review_rejection"]
    lines.append(f"## 🔥 HOT fire-review signals ({len(hot)})")
    lines.append("")
    if not hot:
        lines.append("_No fire-review rejections detected in the current data._")
        lines.append("")
    else:
        permit_by_id = {p.id: p for p in permits}
        for s in hot:
            permit = permit_by_id.get(s.permit_id)
            if not permit:
                continue
            contractor = _contractor_for(session, s.permit_id)
            lines.append(
                f"### {permit.record_number} — {permit.address_line1 or 'address n/a'} "
                f"(cycle {s.cycle_number})"
            )
            if contractor:
                lines.append(
                    f"- Contractor: **{contractor.raw_name}**"
                    + (f" — {contractor.phone}" if contractor.phone else "")
                )
            if s.status:
                lines.append(f"- Review status: {s.status}")
            if s.comment_text:
                lines.append(f"- Reviewer comment: > {s.comment_text}")
            lines.append(f"- Suggested opener: {_opening_line(permit, s)}")
            lines.append("")

    # 2) Highest-scoring new permits.
    lines.append("## Top scored permits")
    lines.append("")
    lines.append("| Score | Permit | Type | Valuation | Address | Why |")
    lines.append("|------:|--------|------|----------:|---------|-----|")
    for sl in scored[:top_n]:
        p = sl.permit
        why = ", ".join(f"{k}+{v}" for k, v in sl.breakdown.items()) or "—"
        val = f"${p.valuation:,.0f}" if p.valuation else ""
        lines.append(
            f"| {sl.score:g} | {p.record_number} | {p.record_subtype or p.record_type or ''} "
            f"| {val} | {p.address_line1 or ''} | {why} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_digest(session: Session, today: date | None = None, out_dir: Path = DIGEST_DIR) -> Path:
    today = today or date.today()
    text = build_digest(session, today=today)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{today.isoformat()}.md"
    path.write_text(text)
    return path


def export_permits_csv(session: Session, today: date | None = None) -> str:
    """Flat CSV for CRM import: one row per permit with its score."""
    today = today or date.today()
    scorer = Scorer()
    signals_by_permit: dict[int, list[Signal]] = {}
    for s in session.exec(select(Signal)).all():
        signals_by_permit.setdefault(s.permit_id, []).append(s)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "record_number",
            "score",
            "record_type",
            "worktype",
            "status",
            "valuation",
            "address",
            "applied_date",
            "contractor",
            "contractor_phone",
            "reasons",
        ]
    )
    permits = session.exec(select(Permit)).all()
    scored = [
        scorer.score_permit(p, signals=signals_by_permit.get(p.id, []), today=today)
        for p in permits
    ]
    scored.sort(key=lambda sl: sl.score, reverse=True)
    for sl in scored:
        p = sl.permit
        contractor = _contractor_for(session, p.id)
        writer.writerow(
            [
                p.record_number,
                sl.score,
                p.record_type or "",
                p.record_subtype or "",
                p.status or "",
                p.valuation or "",
                p.address_line1 or "",
                p.applied_date.isoformat() if p.applied_date else "",
                contractor.raw_name if contractor else "",
                contractor.phone if contractor and contractor.phone else "",
                "; ".join(f"{k}+{v}" for k, v in sl.breakdown.items()),
            ]
        )
    return buf.getvalue()
