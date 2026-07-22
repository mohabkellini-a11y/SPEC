"""Harvest orchestration: run an adapter over a date window and persist results.

A run never silently returns zero. If an adapter yields nothing it is surfaced
loudly to the caller (count == 0), and the CLI turns that into a warning/exit
code — the "silent empty list" failure mode SPEC §2 forbids.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import structlog
from sqlmodel import Session

from . import db as dbmod
from .adapters.registry import get_adapter
from .config import load_jurisdiction
from .review_monitor import ReviewMonitor

log = structlog.get_logger()


@dataclass
class HarvestResult:
    slug: str
    since: date
    until: date
    seen: int = 0
    created: int = 0
    updated: int = 0
    fire_related: int = 0
    hot_signals: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.seen > 0 and not self.errors


def harvest_jurisdiction(
    slug: str,
    since: date,
    until: date,
    engine,
    limit: int | None = None,
    record_types: list[str] | None = None,
) -> HarvestResult:
    config = load_jurisdiction(slug)
    if not config.get("enabled", False):
        raise RuntimeError(
            f"jurisdiction '{slug}' is not enabled in config "
            f"(platform '{config.get('platform')}'): {config.get('notes', '').strip()}"
        )

    adapter = get_adapter(slug, config)
    result = HarvestResult(slug=slug, since=since, until=until)

    with Session(engine) as session:
        juris = dbmod.get_or_create_jurisdiction(
            session,
            slug=slug,
            display_name=config["display_name"],
            platform=config["platform"],
            base_url=config["base_url"],
            timezone=config.get("timezone", "America/New_York"),
            notes=(config.get("notes") or "").strip() or None,
        )

        monitor = ReviewMonitor()
        try:
            for stub in adapter.search(since, until, record_types):
                detail = adapter.fetch_detail(stub)
                permit, created = dbmod.persist_detail(session, juris.id, detail)
                result.seen += 1
                result.created += int(created)
                result.updated += int(not created)
                if hasattr(adapter, "is_fire_related") and adapter.is_fire_related(detail):
                    result.fire_related += 1
                # Module 2: diff review rows and fire (deduplicated) HOT signals.
                result.hot_signals += len(monitor.detect_new(session, permit.id, today=until))
                if limit is not None and result.seen >= limit:
                    break
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            result.errors.append(f"{type(exc).__name__}: {exc}")
            log.error("harvest.error", slug=slug, error=str(exc))
        finally:
            if hasattr(adapter, "close"):
                adapter.close()

    log.info(
        "harvest.done",
        slug=slug,
        seen=result.seen,
        created=result.created,
        updated=result.updated,
        fire_related=result.fire_related,
        hot_signals=result.hot_signals,
    )
    return result
