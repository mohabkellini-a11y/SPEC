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
licensing_app = typer.Typer(add_completion=False, help="Module 3 licensing collectors.")
app.add_typer(licensing_app, name="licensing")


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
        f"hot_signals={result.hot_signals} ({since_d} .. {until_d})"
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
def digest(
    db_path: Path = typer.Option(dbmod.DEFAULT_DB_PATH, "--db"),
    out_dir: Path = typer.Option(Path("data/digests"), "--out", help="Digest output dir."),
    on_date: str | None = typer.Option(None, "--date", help="ISO date label (default: today)."),
    stdout: bool = typer.Option(False, "--stdout", help="Print instead of writing a file."),
) -> None:
    """Write the daily Markdown brief: HOT fire-review signals then top permits."""
    from .reports import build_digest, write_digest

    day = date.fromisoformat(on_date) if on_date else date.today()
    engine = dbmod.get_engine(db_path)
    dbmod.init_db(engine)
    with Session(engine) as session:
        if stdout:
            typer.echo(build_digest(session, today=day))
        else:
            path = write_digest(session, today=day, out_dir=out_dir)
            typer.echo(f"wrote {path}")


@app.command()
def export(
    db_path: Path = typer.Option(dbmod.DEFAULT_DB_PATH, "--db"),
    out: Path | None = typer.Option(None, "--out", help="CSV path (default: stdout)."),
) -> None:
    """Export scored permits as a flat CSV for CRM import."""
    from .reports import export_permits_csv

    engine = dbmod.get_engine(db_path)
    dbmod.init_db(engine)
    with Session(engine) as session:
        csv_text = export_permits_csv(session)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(csv_text)
        typer.echo(f"wrote {out}")
    else:
        typer.echo(csv_text)


@licensing_app.command("import-dbpr")
def licensing_import_dbpr(
    file: Path | None = typer.Option(None, "--file", help="Local DBPR CSV (skip download)."),
    download: bool = typer.Option(False, "--download", help="Download the live DBPR extract."),
    limit: int | None = typer.Option(None, "--limit", help="Import only first N rows."),
    db_path: Path = typer.Option(dbmod.DEFAULT_DB_PATH, "--db"),
) -> None:
    """Import the DBPR construction licensee bulk CSV into person/company tables."""
    from .licensing import dbpr
    from .licensing.importer import import_license_records

    engine = dbmod.get_engine(db_path)
    dbmod.init_db(engine)

    if download:
        dest = Path("data/dbpr/CONSTRUCTIONLICENSE_1.csv")
        typer.echo(f"downloading DBPR extract -> {dest} ...")
        dbpr.download(dest)
        file = dest
    if not file:
        typer.secho("provide --file PATH or --download", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    records = dbpr.parse_csv_file(file)
    with Session(engine) as session:
        stats_ = import_license_records(session, records, limit=limit)
    typer.echo(
        f"DBPR import: persons +{stats_.persons_created}/~{stats_.persons_updated} "
        f"companies +{stats_.companies_created}/~{stats_.companies_updated} "
        f"review_queue={stats_.queued_for_review}"
    )


@licensing_app.command("import-fbpe")
def licensing_import_fbpe(
    pe_file: Path | None = typer.Option(None, "--pe-file", help="PE roster CSV."),
    ca_file: Path | None = typer.Option(
        None, "--ca-file", help="Certificate-of-Authorization CSV."
    ),
    db_path: Path = typer.Option(dbmod.DEFAULT_DB_PATH, "--db"),
) -> None:
    """Import FBPE PE / CA rosters and set firm CA / FP-PE flags (Target List B)."""
    from .licensing import fbpe

    if not pe_file and not ca_file:
        typer.secho("provide --pe-file and/or --ca-file", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    engine = dbmod.get_engine(db_path)
    dbmod.init_db(engine)
    with Session(engine) as session:
        pe_n = 0
        ca_stats = None
        if pe_file:
            pe_n = fbpe.apply_pe_records(session, fbpe.parse_pe_roster(pe_file.read_text()))
        if ca_file:
            ca_stats = fbpe.apply_ca_records(session, fbpe.parse_ca_roster(ca_file.read_text()))
    msg = f"FBPE import: +{pe_n} PE persons"
    if ca_stats:
        msg += (
            f"; {ca_stats.ca_firms_flagged} firms flagged has_engineering_ca, "
            f"{ca_stats.fp_pe_firms} with an FP PE on record"
        )
    typer.echo(msg)


@licensing_app.command("import-sfm")
def licensing_import_sfm(
    file: Path = typer.Option(..., "--file", help="SFM fire-protection contractor roster CSV."),
    db_path: Path = typer.Option(dbmod.DEFAULT_DB_PATH, "--db"),
) -> None:
    """Import an SFM (State Fire Marshal) FP-contractor roster; sets is_fp_contractor (Target List A)."""
    from .licensing import sfm

    engine = dbmod.get_engine(db_path)
    dbmod.init_db(engine)
    with Session(engine) as session:
        stats_ = sfm.apply_sfm_records(session, sfm.parse_sfm_roster(file.read_text()))
    typer.echo(
        f"SFM import: {stats_.firms_flagged} firms flagged is_fp_contractor "
        f"(+{stats_.companies_created} new)"
    )


@app.command()
def targets(
    list_: str = typer.Option("fire-active", "--list", help="A, B, or fire-active."),
    county: str | None = typer.Option(None, "--county", help="Jurisdiction slug filter."),
    months: int = typer.Option(12, "--months", help="Trailing permit-volume window."),
    limit: int = typer.Option(50, "--limit"),
    out: Path | None = typer.Option(None, "--out", help="CSV path (default: stdout)."),
    resolve_first: bool = typer.Option(
        True, "--resolve/--no-resolve", help="Resolve permit contractors into companies first."
    ),
    db_path: Path = typer.Option(dbmod.DEFAULT_DB_PATH, "--db"),
) -> None:
    """Emit Target List A/B (or a permit-derived fire-active ranking) as CSV."""
    from .enrich.companies import resolve_permit_contractors
    from .targets import target_list, to_csv

    engine = dbmod.get_engine(db_path)
    dbmod.init_db(engine)
    with Session(engine) as session:
        if resolve_first:
            rstats = resolve_permit_contractors(session)
            typer.echo(
                f"resolved {rstats.parties_seen} parties "
                f"(+{rstats.companies_created} companies, {rstats.linked} linked)",
                err=True,
            )
        rows = target_list(session, list_, county_slug=county, months=months, limit=limit)
        csv_text = to_csv(rows)

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(csv_text)
        typer.echo(f"wrote {out} ({len(rows)} rows)")
    else:
        typer.echo(csv_text)
    if not rows and list_.lower() in ("a", "b"):
        typer.secho(
            f"  (list {list_.upper()} is empty — needs the SFM/FBPE licensing flags; "
            "run the licensing collectors to populate is_fp_contractor / has_engineering_ca.)",
            fg=typer.colors.YELLOW,
            err=True,
        )


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
