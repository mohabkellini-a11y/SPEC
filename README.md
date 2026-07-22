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

# Harvest commercial permits (Orlando is the verified, enabled source)
eversafe-leads harvest --jurisdiction orlando --since 2026-06-01

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
- Later phases: remaining adapters, review differ (Module 2), licensing +
  target lists (Module 3), seal index (Module 4), scoring/digest/exports.

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
