"""EverSafe Leads CLI (typer)."""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import structlog
import typer
from sqlmodel import Session, func, select

from . import db as dbmod
from .config import load_jurisdictions
from .harvest import harvest_jurisdiction
from .models import Jurisdiction, Permit

app = typer.Typer(
    add_completion=False,
    help="Lead intelligence for a Central Florida fire protection engineering practice.",
)


def _configure_logging(verbose: bool) -> None:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if verbose else logging.INFO
        ),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    _configure_logging(verbose)


@app.command()
def jurisdictions() -> None:
    """List configured jurisdictions and whether each is enabled."""
    data = load_jurisdictions()
    for slug, cfg in data.items():
        flag = "on " if cfg.get("enabled") else "off"
        typer.echo(
            f"[{flag}] {slug:10s} {cfg.get('platform', ''):16s} {cfg.get('display_name', '')}"
        )


@app.command()
def harvest(
    jurisdiction: str = typer.Option(..., "--jurisdiction", "-j", help="Jurisdiction slug."),
    since: str | None = typer.Option(None, "--since", help="ISO date (default: 7 days ago)."),
    until: str | None = typer.Option(None, "--until", help="ISO date (default: today)."),
    limit: int | None = typer.Option(None, "--limit", help="Stop after N permits."),
    db_path: Path = typer.Option(dbmod.DEFAULT_DB_PATH, "--db", help="SQLite path."),
) -> None:
    """Pull commercial permits for a jurisdiction into the local DB."""
    since_d = date.fromisoformat(since) if since else date.today() - timedelta(days=7)
    until_d = date.fromisoformat(until) if until else date.today()

    engine = dbmod.get_engine(db_path)
    dbmod.init_db(engine)

    result = harvest_jurisdiction(jurisdiction, since_d, until_d, engine, limit=limit)

    typer.echo(
        f"\n{jurisdiction}: seen={result.seen} created={result.created} "
        f"updated={result.updated} fire_related={result.fire_related} "
        f"({since_d} .. {until_d})"
    )
    if result.errors:
        for err in result.errors:
            typer.secho(f"  ERROR: {err}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if result.seen == 0:
        typer.secho(
            "  WARNING: zero permits returned — check the date window / filters, "
            "this is not necessarily a successful run.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=2)


@app.command()
def stats(db_path: Path = typer.Option(dbmod.DEFAULT_DB_PATH, "--db")) -> None:
    """Show row counts per jurisdiction in the DB."""
    engine = dbmod.get_engine(db_path)
    dbmod.init_db(engine)
    with Session(engine) as session:
        total = session.exec(select(func.count()).select_from(Permit)).one()
        typer.echo(f"permits: {total}")
        for juris in session.exec(select(Jurisdiction)).all():
            n = session.exec(
                select(func.count()).select_from(Permit).where(Permit.jurisdiction_id == juris.id)
            ).one()
            typer.echo(f"  {juris.slug:10s} {n}")


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
