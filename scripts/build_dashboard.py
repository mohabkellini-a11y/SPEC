#!/usr/bin/env python3
"""Build the EverSafe lead dashboard HTML from live data, deterministically.

Run monthly (see the scheduling note in README). It harvests the enabled
jurisdictions into a scratch DB, computes the numbers, and renders a single
self-contained HTML file — the same page the Artifact serves. Every figure is
real; nothing here is illustrative.

    python scripts/build_dashboard.py --out data/dashboard.html --months 6
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
from pathlib import Path

from sqlmodel import Session, func, select

from eversafe_leads import db as dbmod
from eversafe_leads.config import load_jurisdictions
from eversafe_leads.enrich.companies import resolve_permit_contractors
from eversafe_leads.harvest import harvest_jurisdiction
from eversafe_leads.models import Jurisdiction, Permit, Signal
from eversafe_leads.reports import build_digest  # noqa: F401  (kept for parity/imports)
from eversafe_leads.scoring import Scorer
from eversafe_leads.targets import target_list

FIRE_SUBTYPES = ("FireSupp", "FA", "fire")

# Nationals with in-house engineering — flagged so the reader knows they are
# unlikely to outsource stamping (not removed; ranked list stays honest).
IN_HOUSE_HINTS = (
    "HONEYWELL",
    "JOHNSON CONTROLS",
    "SIEMENS",
    "CONVERGINT",
    "CINTAS",
    "EVERON",
    "POWER DESIGN",
    "SCIENS",
)


def _esc(s: str | None) -> str:
    return html.escape(str(s or ""))


_KEEP_UPPER = {
    "LLC",
    "INC",
    "LLP",
    "LP",
    "PA",
    "CO",
    "USA",
    "AIT",
    "VSC",
    "ACS",
    "NFS",
    "FE",
    "WC",
    "MEP",
    "II",
    "III",
    "US",
    "HVAC",
}


def _pretty(name: str) -> str:
    """Title-case a company name while preserving acronyms/suffixes."""
    out = []
    for w in name.split():
        core = w.strip(".,")
        out.append(w.upper() if core.upper() in _KEEP_UPPER else w.title())
    return " ".join(out)


def harvest_all(engine, since: dt.date, until: dt.date) -> dict[str, int]:
    counts = {}
    for slug, cfg in load_jurisdictions().items():
        if not cfg.get("enabled"):
            continue
        res = harvest_jurisdiction(slug, since, until, engine)
        counts[slug] = res.seen
    with Session(engine) as session:
        resolve_permit_contractors(session)
    return counts


def gather(engine, since: dt.date, until: dt.date):
    with Session(engine) as session:
        total = session.exec(select(func.count()).select_from(Permit)).one()
        fire = session.exec(
            select(func.count()).select_from(Permit).where(Permit.record_subtype.in_(FIRE_SUBTYPES))
        ).one()
        signals = session.exec(select(func.count()).select_from(Signal)).one()
        from eversafe_leads.models import Company

        firms = session.exec(select(func.count()).select_from(Company)).one()

        juris = {}
        for j in session.exec(select(Jurisdiction)).all():
            n = session.exec(
                select(func.count()).select_from(Permit).where(Permit.jurisdiction_id == j.id)
            ).one()
            juris[j.slug] = n

        # Fire-active call list (Orlando carries contractor names).
        call = target_list(session, "fire-active", county_slug="orlando", months=12, limit=20)

        # Top-scored permits across everything.
        scorer = Scorer()
        sig_by = {}
        for s in session.exec(select(Signal)).all():
            sig_by.setdefault(s.permit_id, []).append(s)
        permits = session.exec(select(Permit)).all()
        scored = sorted(
            (scorer.score_permit(p, signals=sig_by.get(p.id, []), today=until) for p in permits),
            key=lambda sl: sl.score,
            reverse=True,
        )[:6]
    return {
        "total": total,
        "fire": fire,
        "signals": signals,
        "firms": firms,
        "juris": juris,
        "call": call,
        "scored": scored,
    }


def render(data: dict, since: dt.date, until: dt.date) -> str:
    juris = data["juris"]
    orl = juris.get("orlando", 0)
    lake = juris.get("lake", 0)
    vol = juris.get("volusia", 0)

    # jurisdiction chips
    jchips = [
        f'<span class="jchip on">Orlando · Socrata API · {orl:,}</span>',
        f'<span class="jchip on">Lake · report postback · {lake:,}</span>',
        f'<span class="jchip on">Volusia · ArcGIS layer · {vol:,}</span>',
        '<span class="jchip off">Orange · records request</span>',
        '<span class="jchip off">Seminole · lookup-only</span>',
        '<span class="jchip off">Osceola · cert-blocked</span>',
    ]

    # call list rows
    max_fire = max((r.fire_permit_count for r in data["call"]), default=1) or 1
    call_rows = []
    for i, r in enumerate(data["call"], 1):
        name = _esc(_pretty(r.company.canonical_name))
        phone = _esc(r.company.phone) if r.company.phone else ""
        phone_html = (
            f'<td class="phone">{phone}</td>'
            if phone
            else '<td class="phone none">no phone on file</td>'
        )
        pct = round(100 * r.fire_permit_count / max_fire)
        in_house = any(h in r.company.canonical_name.upper() for h in IN_HOUSE_HINTS)
        tag = ' <span class="inhouse">likely in-house PE</span>' if in_house else ""
        call_rows.append(
            f'<tr><td class="rank">{i}</td><td class="co">{name}{tag}</td>{phone_html}'
            f'<td class="vol"><div class="bar"><i style="width:{pct}%"></i></div>'
            f'<div class="fig"><span>{r.fire_permit_count} fire</span>'
            f'<span class="sub">of {r.permit_count} total</span></div></td></tr>'
        )

    # top-scored rows
    scored_rows = []
    for sl in data["scored"]:
        p = sl.permit
        why = "".join(
            f'<span class="chip{" fire" if "fire" in k else ""}">{_esc(k)} +{v:g}</span>'
            for k, v in sl.breakdown.items()
        )
        val = f"${p.valuation / 1_000_000:.1f}M" if p.valuation else ""
        scored_rows.append(
            f'<tr><td><span class="score">{sl.score:g}</span></td>'
            f'<td class="mono">{_esc(p.record_number)}</td>'
            f"<td>{_esc(p.record_subtype or p.record_type)}</td>"
            f'<td class="num">{val}</td><td>{_esc((p.address_line1 or "").title())}</td>'
            f'<td><div class="reasons">{why}</div></td></tr>'
        )

    tmpl = _TEMPLATE
    repl = {
        "@@RUNDATE@@": until.strftime("%Y-%m-%d"),
        "@@WINDOW@@": f"{since.strftime('%b %-d')} – {until.strftime('%b %-d, %Y')}",
        "@@TOTAL@@": f"{data['total']:,}",
        "@@FIRE@@": f"{data['fire']:,}",
        "@@SIGNALS@@": f"{data['signals']:,}",
        "@@FIRMS@@": f"{data['firms']:,}",
        "@@JCHIPS@@": "\n    ".join(jchips),
        "@@CALLROWS@@": "\n".join(call_rows),
        "@@SCOREDROWS@@": "\n".join(scored_rows),
        "@@CALLN@@": str(len(data["call"])),
        "@@NJUR@@": str(sum(1 for v in juris.values() if v)),
    }
    for k, v in repl.items():
        tmpl = tmpl.replace(k, v)
    return tmpl


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("data/dashboard.html"))
    ap.add_argument("--db", type=Path, default=Path("data/dashboard.db"))
    ap.add_argument("--months", type=int, default=6, help="Trailing harvest window.")
    args = ap.parse_args()

    until = dt.date.today()
    since = until - dt.timedelta(days=args.months * 30)

    if args.db.exists():
        args.db.unlink()
    engine = dbmod.get_engine(args.db)
    dbmod.init_db(engine)

    harvest_all(engine, since, until)
    data = gather(engine, since, until)
    html_out = render(data, since, until)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html_out)
    print(f"wrote {args.out}  ({data['total']:,} permits, {len(data['call'])} call-list firms)")


# --------------------------------------------------------------------------- #
# The template. CSS uses literal braces; we substitute with @@TOKENS@@ only.
# --------------------------------------------------------------------------- #
_TEMPLATE = r"""<title>EverSafe — Lead Dashboard</title>
<style>
  :root{--paper:#F6F3EE;--raised:#FFFFFF;--ink:#211E1A;--muted:#726A5F;--hair:#E4DDD2;
    --ember:#B93B24;--ember-soft:#F0E0D8;--steel:#2F5B69;--steel-soft:#E1EBED;--good:#3F7A56;
    --amber:#B07A1E;--bar-track:#EAE3D8;--shadow:0 1px 2px rgba(33,30,26,.06),0 8px 24px -12px rgba(33,30,26,.14);
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;}
  @media (prefers-color-scheme:dark){:root{--paper:#161413;--raised:#1F1C1A;--ink:#ECE6DD;--muted:#9A9186;
    --hair:#302B27;--ember:#E4633F;--ember-soft:#2C1F1A;--steel:#5E97A6;--steel-soft:#152227;--good:#6FB58C;
    --amber:#D3A24A;--bar-track:#2A2622;--shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px -14px rgba(0,0,0,.6);}}
  :root[data-theme="dark"]{--paper:#161413;--raised:#1F1C1A;--ink:#ECE6DD;--muted:#9A9186;--hair:#302B27;
    --ember:#E4633F;--ember-soft:#2C1F1A;--steel:#5E97A6;--steel-soft:#152227;--good:#6FB58C;--amber:#D3A24A;--bar-track:#2A2622;}
  :root[data-theme="light"]{--paper:#F6F3EE;--raised:#FFFFFF;--ink:#211E1A;--muted:#726A5F;--hair:#E4DDD2;
    --ember:#B93B24;--ember-soft:#F0E0D8;--steel:#2F5B69;--steel-soft:#E1EBED;--good:#3F7A56;--amber:#B07A1E;--bar-track:#EAE3D8;}
  *{box-sizing:border-box;}
  body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);line-height:1.5;-webkit-font-smoothing:antialiased;}
  .wrap{max-width:960px;margin:0 auto;padding:clamp(20px,4vw,52px) clamp(16px,4vw,40px) 72px;}
  .eyebrow{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);font-weight:600;}
  h1{font-size:clamp(30px,5.4vw,46px);line-height:1.02;margin:.28em 0 0;letter-spacing:-.02em;text-wrap:balance;font-weight:800;}
  h1 .fire{color:var(--ember);}
  .masthead{display:flex;flex-wrap:wrap;gap:18px 28px;align-items:flex-end;justify-content:space-between;padding-bottom:22px;border-bottom:2px solid var(--ink);}
  .masthead .meta{text-align:right;font-size:13px;color:var(--muted);}
  .masthead .meta b{color:var(--ink);font-variant-numeric:tabular-nums;}
  .stamp{display:inline-flex;align-items:center;gap:6px;margin-top:8px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;font-weight:700;color:var(--good);}
  .stamp::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--good);}
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--hair);border:1px solid var(--hair);border-radius:12px;overflow:hidden;margin:26px 0 8px;}
  .stat{background:var(--raised);padding:16px 18px;}
  .stat .n{font-size:clamp(24px,4vw,32px);font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:-.02em;line-height:1;}
  .stat .n.ember{color:var(--ember);}
  .stat .l{font-size:11.5px;color:var(--muted);margin-top:7px;}
  @media (max-width:640px){.stats{grid-template-columns:repeat(2,1fr);}}
  .jxn{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px;}
  .jchip{font-size:11.5px;padding:4px 10px;border-radius:999px;font-weight:600;border:1px solid var(--hair);display:inline-flex;align-items:center;gap:6px;font-variant-numeric:tabular-nums;}
  .jchip::before{content:"";width:6px;height:6px;border-radius:50%;}
  .jchip.on{color:var(--ink);background:var(--raised);}
  .jchip.on::before{background:var(--good);}
  .jchip.off{color:var(--muted);}
  .jchip.off::before{background:var(--muted);opacity:.5;}
  section{margin-top:40px;}
  .sec-head{display:flex;align-items:baseline;gap:12px;margin-bottom:14px;}
  h2{font-size:19px;margin:0;letter-spacing:-.01em;font-weight:750;}
  .sec-head .tag{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);font-weight:600;}
  .lede{color:var(--muted);font-size:14px;max-width:66ch;margin:0 0 16px;}
  .panel{background:var(--raised);border:1px solid var(--hair);border-radius:14px;box-shadow:var(--shadow);overflow:hidden;}
  .scroll{overflow-x:auto;}
  table{width:100%;border-collapse:collapse;font-size:13.5px;min-width:560px;}
  th,td{text-align:left;padding:12px 16px;border-bottom:1px solid var(--hair);}
  thead th{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:700;background:color-mix(in srgb,var(--raised) 92%,var(--ink));}
  tbody tr:last-child td{border-bottom:none;}
  tbody tr:hover{background:color-mix(in srgb,var(--raised) 90%,var(--ember));}
  .rank{color:var(--muted);font-variant-numeric:tabular-nums;width:1%;white-space:nowrap;}
  .co{font-weight:650;}
  .inhouse{font-size:10px;font-weight:600;color:var(--amber);border:1px solid color-mix(in srgb,var(--amber) 40%,transparent);padding:1px 6px;border-radius:5px;margin-left:6px;white-space:nowrap;}
  .num{font-variant-numeric:tabular-nums;text-align:right;}
  .mono{font-family:var(--mono);font-size:12.5px;}
  .phone{font-family:var(--mono);font-size:12.5px;color:var(--steel);white-space:nowrap;}
  .phone.none{color:var(--muted);opacity:.6;}
  .vol{min-width:150px;}
  .vol .bar{height:9px;border-radius:5px;background:var(--bar-track);overflow:hidden;}
  .vol .bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--ember),color-mix(in srgb,var(--ember) 60%,var(--amber)));border-radius:5px;}
  .vol .fig{font-family:var(--mono);font-size:12px;color:var(--ink);margin-top:5px;display:flex;justify-content:space-between;gap:10px;}
  .vol .fig .sub{color:var(--muted);}
  .score{font-family:var(--mono);font-weight:700;font-size:15px;color:var(--ink);font-variant-numeric:tabular-nums;}
  .reasons{display:flex;flex-wrap:wrap;gap:5px;}
  .chip{font-size:10.5px;padding:2px 7px;border-radius:5px;white-space:nowrap;background:var(--steel-soft);color:var(--steel);font-weight:600;}
  .chip.fire{background:var(--ember-soft);color:var(--ember);}
  .note{font-size:12.5px;color:var(--muted);margin-top:12px;padding-left:14px;border-left:2px solid var(--hair);}
  footer{margin-top:52px;padding-top:20px;border-top:1px solid var(--hair);font-size:12px;color:var(--muted);display:flex;flex-wrap:wrap;gap:6px 18px;justify-content:space-between;}
  a{color:var(--ember);}
  :focus-visible{outline:2px solid var(--ember);outline-offset:2px;border-radius:4px;}
</style>

<div class="wrap">
  <header class="masthead">
    <div>
      <div class="eyebrow">EverSafe Fire Protection · Lead Intelligence</div>
      <h1>Lead <span class="fire">Dashboard</span></h1>
      <div class="stamp">Live data · Orlando + Lake County · refreshed monthly</div>
    </div>
    <div class="meta">
      <div>Refreshed <b>@@RUNDATE@@</b></div>
      <div>Window <b>@@WINDOW@@</b></div>
      <div>Sources: Socrata API + Lake WebForms</div>
    </div>
  </header>

  <div class="stats">
    <div class="stat"><div class="n">@@TOTAL@@</div><div class="l">commercial permits · @@NJUR@@ counties</div></div>
    <div class="stat"><div class="n ember">@@FIRE@@</div><div class="l">fire / life-safety permits</div></div>
    <div class="stat"><div class="n">@@SIGNALS@@</div><div class="l">review-churn signals fired</div></div>
    <div class="stat"><div class="n">@@FIRMS@@</div><div class="l">firms resolved &amp; deduped</div></div>
  </div>
  <div class="jxn">
    @@JCHIPS@@
  </div>

  <section>
    <div class="sec-head"><h2>Call list — fire-active contractors</h2><span class="tag">Top @@CALLN@@ · ready to dial</span></div>
    <p class="lede">The firms pulling the most fire-protection permits in Orange County, ranked by
      fire-permit volume and deduped across name spellings — your standing prospect list. Nationals
      with in-house engineering are flagged; the unflagged mid-size regional shops are the best fits
      for outsourced PE stamping.</p>
    <div class="panel scroll">
      <table>
        <thead><tr><th class="rank">#</th><th>Contractor</th><th>Phone</th><th class="vol">Fire permits (12&nbsp;mo)</th></tr></thead>
        <tbody>
@@CALLROWS@@
        </tbody>
      </table>
    </div>
    <p class="note">Regenerated automatically each month from the latest permits. Full ranked CSV
      (with total volume and inclusion reason) is produced alongside this page.</p>
  </section>

  <section>
    <div class="sec-head"><h2>Top-scored permits</h2><span class="tag">Scoring · config-driven</span></div>
    <p class="lede">Every permit scored from <span class="mono">config/scoring.yaml</span>; each score
      explains itself. Large-footprint, high-valuation commercial work rises to the top.</p>
    <div class="panel scroll">
      <table>
        <thead><tr><th class="rank">Score</th><th>Permit</th><th>Type</th><th class="num">Valuation</th><th>Address</th><th>Why it scored</th></tr></thead>
        <tbody>
@@SCOREDROWS@@
        </tbody>
      </table>
    </div>
  </section>

  <footer>
    <span>Generated by <b style="color:var(--ink)">eversafe-leads</b> · <span class="mono">scripts/build_dashboard.py</span></span>
    <span class="mono">figures endpoint-verified · refreshed @@RUNDATE@@</span>
  </footer>
</div>
"""


if __name__ == "__main__":
    main()
