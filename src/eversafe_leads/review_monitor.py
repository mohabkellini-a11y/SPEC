"""Module 2 — fire review monitor.

A filter/diff layer over Module 1, not a separate crawler. It compares the
review rows now visible for a permit against what fired before and raises HOT
signals when fire/life-safety review turns sour:

* a fire-department review transitions into a rejection-like status;
* a second-or-later review cycle on a fire discipline;
* a permit stuck in fire review beyond a turnaround threshold.

Dedup is persistent: one alert per permit per review cycle, ever (SPEC §6),
enforced by ``Signal.dedup_key``. Reviewer comment text is captured verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlmodel import Session, select

from .config import load_scoring
from .models import Review, Signal


@dataclass
class HotSignal:
    permit_id: int
    kind: str
    cycle_number: int
    department: str | None
    status: str | None
    comment_text: str | None

    @property
    def dedup_key(self) -> str:
        return f"{self.permit_id}:{self.kind}:{self.cycle_number}:{self.department or ''}"


class ReviewMonitor:
    def __init__(self, scoring: dict | None = None, stuck_days: int = 21) -> None:
        cfg = scoring or load_scoring()
        self.fire_departments = [d.lower() for d in cfg.get("fire_review_departments", [])]
        self.hot_statuses = [s.lower() for s in cfg.get("hot_review_statuses", [])]
        self.stuck_days = stuck_days

    # -- predicates -------------------------------------------------------- #
    def is_fire_department(self, department: str | None) -> bool:
        if not department:
            return False
        d = department.lower()
        return any(token in d for token in self.fire_departments)

    def is_hot_status(self, status: str | None) -> bool:
        if not status:
            return False
        s = status.lower()
        return any(token in s for token in self.hot_statuses)

    # -- core diff --------------------------------------------------------- #
    def evaluate(self, reviews: list[Review], today: date | None = None) -> list[HotSignal]:
        """Return the HOT signals implied by a permit's current review rows.

        Pure function over the review list — no persistence, no dedup. Callers
        that want dedup use :meth:`detect_new`.
        """
        today = today or date.today()
        signals: list[HotSignal] = []
        fire_reviews = [r for r in reviews if self.is_fire_department(r.department)]

        for r in fire_reviews:
            if self.is_hot_status(r.status):
                signals.append(
                    HotSignal(
                        permit_id=r.permit_id,
                        kind="fire_review_rejection",
                        cycle_number=r.cycle_number,
                        department=r.department,
                        status=r.status,
                        comment_text=r.comment_text,
                    )
                )

        # A second-or-later fire review cycle is itself a signal (churn).
        max_cycle = max((r.cycle_number for r in fire_reviews), default=0)
        if max_cycle >= 2:
            latest = max(fire_reviews, key=lambda r: r.cycle_number)
            signals.append(
                HotSignal(
                    permit_id=latest.permit_id,
                    kind="second_fire_cycle",
                    cycle_number=max_cycle,
                    department=latest.department,
                    status=latest.status,
                    comment_text=latest.comment_text,
                )
            )

        # Stuck in fire review: open fire review whose status_date is old and
        # not yet resolved.
        for r in fire_reviews:
            if (
                r.status_date
                and not self.is_hot_status(r.status)
                and (today - r.status_date).days >= self.stuck_days
                and (r.status or "").lower() not in ("approved", "complete", "passed")
            ):
                signals.append(
                    HotSignal(
                        permit_id=r.permit_id,
                        kind="stuck_in_fire_review",
                        cycle_number=r.cycle_number,
                        department=r.department,
                        status=r.status,
                        comment_text=r.comment_text,
                    )
                )
        return signals

    def detect_new(
        self, session: Session, permit_id: int, today: date | None = None
    ) -> list[Signal]:
        """Evaluate a permit's stored reviews and persist only signals not seen
        before. Returns the newly-fired ``Signal`` rows (possibly empty)."""
        reviews = session.exec(select(Review).where(Review.permit_id == permit_id)).all()
        candidates = self.evaluate(list(reviews), today=today)

        new_rows: list[Signal] = []
        for c in candidates:
            exists = session.exec(select(Signal).where(Signal.dedup_key == c.dedup_key)).first()
            if exists:
                continue
            row = Signal(
                permit_id=c.permit_id,
                kind=c.kind,
                cycle_number=c.cycle_number,
                department=c.department,
                status=c.status,
                comment_text=c.comment_text,
                dedup_key=c.dedup_key,
            )
            session.add(row)
            new_rows.append(row)
        if new_rows:
            session.commit()
            for row in new_rows:
                session.refresh(row)
        return new_rows
