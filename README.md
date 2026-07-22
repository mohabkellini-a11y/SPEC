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

# Harvest commercial permits (Orlando is the verified, enabled source).
# This also runs the Module 2 fire-review differ and fires HOT signals.
eversafe-leads harvest --jurisdiction orlando --since 2026-06-01

# Daily brief: HOT fire-review signals first, then top-scored permits.
eversafe-leads digest            # writes data/digests/YYYY-MM-DD.md
eversafe-leads digest --stdout   # or print it

# Flat CSV for CRM import (scored, sorted).
eversafe-leads export --out data/leads.csv

# Import DBPR construction licensees (verified 48MB bulk CSV).
eversafe-leads licensing import-dbpr --download

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
- **Phase 3** — Module 2 fire-review differ + HOT signals (persistent dedup).
- **Phase 6** — scoring engine (`config/scoring.yaml`), daily digest, CSV export.
- **Module 3** — name normalization, entity resolution (exact / fuzzy≥92 /
  85–92 review queue), the verified DBPR construction bulk-CSV collector,
  permit-contractor → company resolution, and target lists (`targets`).
  `fire-active` ranking works today from permit data; Lists A/B are wired and
  become populated once the SFM (FP-contractor) and FBPE (CA / PE) flags land.
- Remaining: SFM + FBPE collectors to fill Target Lists A/B; more jurisdiction
  adapters (Orange → records request; Osceola/Seminole/Lake/Volusia); seal
  index (Module 4).

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
