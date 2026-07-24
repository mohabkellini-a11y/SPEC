# EverSafe Lead Intelligence System

Finds, scores, and delivers commercial fire-protection engineering leads from
Central Florida public records — no ad spend. See `SPEC.md` for the canonical
build spec, `docs/PHASE0_RECON.md` for endpoint reconnaissance, and `CLAUDE.md`
for portal quirks.

> **Data handling.** Collected records may include names, phones, and addresses
> of private individuals. Everything lives in a gitignored `data/` directory —
> the SQLite DB, raw HTML cache, and downloaded PDFs are never committed.

## Install

```bash
uv venv --python 3.11
uv pip install -e ".[dev]"
```

Heavy extras are opt-in: `.[scrape]` (Playwright/selectolax), `.[pdf]`,
`.[ocr]`, `.[address]`.

## Usage

```bash
# List configured jurisdictions (and which are enabled)
eversafe-leads jurisdictions

# Harvest commercial permits. Two live jurisdictions are enabled: orlando
# (Socrata API) and lake (ASP.NET report postback). This also runs the Module 2
# fire-review differ and fires HOT signals.
eversafe-leads harvest --jurisdiction orlando --since 2026-06-01
eversafe-leads harvest --jurisdiction lake --since 2026-06-01

# Daily brief: HOT fire-review signals first, then top-scored permits.
eversafe-leads digest            # writes data/digests/YYYY-MM-DD.md
eversafe-leads digest --stdout   # or print it

# Flat CSV for CRM import (scored, sorted).
eversafe-leads export --out data/leads.csv

# Import DBPR construction licensees (verified 48MB bulk CSV).
eversafe-leads licensing import-dbpr --download

# Import FBPE rosters (PE + Certificate-of-Authorization) — obtained via the
# directory / a Ch. 119 records request. The CA roster sets the CA / FP-PE
# flags that Target List B needs.
eversafe-leads licensing import-fbpe --pe-file pe.csv --ca-file ca.csv

# Import the SFM fire-protection contractor roster (records request / portal
# export). Sets is_fp_contractor — the flag Target List A needs.
eversafe-leads licensing import-sfm --file sfm.csv

# Target List A / B (populated once the SFM / FBPE rosters are imported).
eversafe-leads targets --list A --county orlando --limit 50

# Module 4 — seal index. Extract the sealing engineer from a drawing-set PDF
# (digital signature -> PDF text -> OCR), validate the PE against FBPE data,
# and attach it to a harvested permit. Then report sealing engineers.
eversafe-leads ingest-seals --pdf plans.pdf --permit 2026070589 -j lake
eversafe-leads seals --county lake --since 2025-01-01

# Target lists: fire-active contractors by permit volume (works today);
# A/B become populated once the SFM/FBPE flags are collected.
eversafe-leads targets --list fire-active --county orlando --limit 25

# Row counts in the DB
eversafe-leads stats
```

`harvest` exits non-zero and warns loudly on zero rows — an empty result is
never treated as success (SPEC §2).

### Configuration

- `config/jurisdictions.yaml` — one block per jurisdiction (platform, base_url,
  rate limit, enabled, Phase 0 notes). Adding a jurisdiction on an existing
  platform is a config edit only.
- `config/scoring.yaml` — lead-scoring weights, editable without code.

Optional: set `SOCRATA_APP_TOKEN` for higher Socrata throughput (never required).

## Status

- **Phase 0** — reconnaissance complete and endpoint-verified (`docs/PHASE0_RECON.md`).
- **Phase 1** — repo, models, DB, CLI, config, and the City of Orlando (Socrata)
  adapter. `harvest --jurisdiction orlando` writes real rows.
- **Phase 2 (in progress)** — Lake County adapter live (ASP.NET WebForms
  postback over `permits_issued.aspx`); a second jurisdiction harvesting real
  commercial fire permits.
- **Phase 3** — Module 2 fire-review differ + HOT signals (persistent dedup).
- **Phase 6** — scoring engine (`config/scoring.yaml`), daily digest, CSV export.
- **Module 3** — name normalization, entity resolution (exact / fuzzy≥92 /
  85–92 review queue), the verified DBPR construction bulk-CSV collector,
  permit-contractor → company resolution, and target lists (`targets`).
  `fire-active` ranking works today from permit data; Lists A/B are wired and
  become populated once the SFM (FP-contractor) and FBPE (CA / PE) flags land.
- **FBPE + SFM collectors** — roster imports set `has_engineering_ca` /
  `has_fp_pe_on_record` (FBPE) and `is_fp_contractor` (SFM), which light up
  **both Target List A and Target List B**. Live search for both is a JS-portal /
  crawl-delayed path, so rosters (records request / export) are the honest cheap
  path; the analysis machinery is complete.
- **Module 4 (seal index)** — extraction cascade (digital signature 0.98 → PDF
  text 0.85 → OCR 0.60), PE/CA regex, FBPE cross-validation, and the sealing-
  engineer intelligence report (`seals`). Feed drawing sets with `ingest-seals`.
  Note: no enabled jurisdiction exposes downloadable approved drawing sets yet,
  so the auto-download step awaits a document-serving portal (e.g. Accela Civic
  Access document endpoints) — the extraction/intelligence pipeline is complete
  and works on any PDF you provide today.
- Remaining: more jurisdiction adapters (Orange → records request; Osceola/
  Seminole/Volusia); document auto-download for Module 4; incremental watermark
  crawl.

## Lead dashboard

`scripts/build_dashboard.py` harvests the enabled jurisdictions and renders a
single self-contained HTML dashboard — the monthly call list of fire-active
contractors (with phones, nationals flagged) plus the top-scored permits. Every
figure is live; nothing illustrative.

```bash
python scripts/build_dashboard.py --out data/dashboard.html --months 6
```

It is designed to be regenerated monthly and republished to the same hosted
dashboard page. A companion CSV call list comes from
`eversafe-leads targets --list fire-active --county orlando --out <file>`.

## Scheduling (v1: run it yourself, no hosted infra)

Daily harvest via cron (adjust paths):

```cron
# 6:15 AM daily — harvest Orlando's last 7 days
15 6 * * * cd /path/to/eversafe-leads && .venv/bin/eversafe-leads harvest -j orlando >> data/harvest.log 2>&1
```

systemd timer equivalent (`~/.config/systemd/user/eversafe-harvest.{service,timer}`):

```ini
# eversafe-harvest.service
[Unit]
Description=EverSafe daily permit harvest
[Service]
Type=oneshot
WorkingDirectory=/path/to/eversafe-leads
ExecStart=/path/to/eversafe-leads/.venv/bin/eversafe-leads harvest -j orlando
```

```ini
# eversafe-harvest.timer
[Unit]
Description=Run EverSafe harvest daily
[Timer]
OnCalendar=*-*-* 06:15:00
Persistent=true
[Install]
WantedBy=timers.target
```

Enable with `systemctl --user enable --now eversafe-harvest.timer`.
