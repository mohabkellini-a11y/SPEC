# EverSafe Lead Intelligence System — Build Spec

> This file is the canonical build specification, preserved verbatim from the
> project kickoff. See `docs/PHASE0_RECON.md` for reconnaissance findings and
> `CLAUDE.md` for working notes and portal quirks.

## 1. Role and mission

You are building a lead intelligence system for a solo Florida fire protection
engineering practice (EverSafe Fire Protection, eversafe-fire.com). The practice
sells PE-stamped sprinkler and fire alarm design, third-party plan review and
stamping, code consulting, and FDS modeling to MEP firms, architects,
design-build fire protection contractors, GCs, and building owners.

The system's job is to find, score, and deliver commercial leads from public
records across Central Florida, with no ad spend. It has four modules:

1. **Permit harvester** — pull commercial building permit records from Central
   Florida jurisdiction portals.
2. **Review-status monitor** — detect permits whose fire/life-safety review
   returned corrections, comments, or rejection.
3. **Capability gap analysis** — cross-reference licensed fire protection
   contractors and design firms against engineering licensure records to find
   firms that must outsource PE stamping.
4. **Seal index** — extract sealing engineer identity from approved/permitted
   drawing sets to map who stamps what, for whom, and where.

Target jurisdictions for v1: City of Orlando, Orange County, Seminole County,
Osceola County, Lake County, Volusia County. Design so that adding City of
Kissimmee, Sanford, Winter Park, Altamonte Springs, Ocoee, Apopka, and Winter
Garden later is a config change plus one adapter class, not a refactor.

## 2. Non-negotiable constraints

**Honesty over completion.** Never invent a URL, API endpoint, form field, CSS
selector, or license-search path. If you do not know it, go find it with a real
HTTP request or browser session and report what you actually observed. If you
cannot find it, say so and stop. A module that honestly reports "Lake County
requires a session cookie I could not obtain" is worth more than one that
silently returns zero rows.

**No mock data in production paths.** Fixtures live in `tests/fixtures/` and are
only used by tests. If a scraper returns nothing, it raises or logs a loud
warning — it never returns an empty list that looks like a successful run.

**Respectful collection.** Before writing any scraper: read the target's
`robots.txt` and terms of use, and report what they say. Then:

- Default to no more than 1 request every 2 seconds per host, with jitter; make
  this configurable per jurisdiction.
- Set a descriptive, honest `User-Agent` with a contact email.
- Never attempt to access anything behind a login, paywall, or CAPTCHA. If a
  portal requires authentication, stop and flag it for a records request.
- Cache aggressively; never re-fetch a record you already have unless its status
  changed.
- Do not scrape LinkedIn, ZoomInfo, Apollo, or any social platform. Out of
  scope, permanently.

**Prefer the cheap path.** In Phase 0, for each jurisdiction, check in this order
and report which exists:

1. An official documented API (Accela Civic Platform / Construct APIs, Tyler
   EnerGov CSS REST endpoints, OpenGov, Socrata/ArcGIS open data portals).
2. A bulk download, open data portal, or GIS feature service.
3. A public records request contact (Florida Chapter 119). Several counties will
   hand over a permit extract on request — flag these as "ask, don't scrape."
4. HTML scraping, as a last resort.

**Privacy.** Scraped data may include names, phone numbers, and addresses of
private individuals. Store it in a gitignored `data/` directory. Never commit the
database, raw HTML cache, or downloaded PDFs. Add these to `.gitignore` in the
first commit.

I am not asking you for legal advice and you should not give it. Flag anything
that looks like a terms-of-use conflict and let me decide.

## 3. Tech stack

- Python 3.11+, dependency management with `uv`.
- `httpx` for HTTP; `playwright` (Chromium) only for portals that genuinely
  require JS or ASP.NET postback flows.
- `selectolax` or `beautifulsoup4` for HTML parsing.
- `pydantic` v2 for all record models; parsing failures are validation errors,
  not silent `None`s.
- `SQLModel` over SQLite for storage. One file at `data/leads.db`.
- `typer` for the CLI, `structlog` for logging, `tenacity` for retries.
- `rapidfuzz` for entity resolution, `usaddress` for address parsing.
- `pdfplumber` + `pypdf`/`pikepdf` for PDF text and metadata; `pymupdf` for
  rasterization; `pytesseract` or `paddleocr` for OCR.
- `pytest` with recorded HTML/JSON fixtures. `ruff` for lint and format.
- No Docker in v1. No web framework in v1.

## 4. Repository structure

```
eversafe-leads/
  SPEC.md                  # this document
  CLAUDE.md                # working notes, portal quirks, gotchas
  pyproject.toml
  config/
    jurisdictions.yaml     # one block per jurisdiction: platform, base_url, rate_limit, enabled
    scoring.yaml           # lead scoring weights, editable without code changes
  src/eversafe_leads/
    models.py
    db.py
    adapters/
      base.py              # JurisdictionAdapter ABC
      accela.py
      etrakit.py
      cityview.py
      energov.py
      arcgis.py
    licensing/
      sfm.py               # State Fire Marshal contractor licenses
      fbpe.py              # engineer + certificate of authorization records
      dbpr.py              # construction contractors, architects
    seals/
      digital_sig.py
      ocr.py
    enrich/
      normalize.py         # company/name/address normalization
      resolve.py           # entity resolution across sources
    scoring.py
    reports.py
    cli.py
  tests/
    fixtures/
```

## 5. Data model

Define these as SQLModel tables. Every row carries `source_url`, `first_seen_at`,
`last_seen_at`, and `raw_payload` (JSON) so nothing is lossy.

- **jurisdiction** — slug, display name, platform, base_url, timezone, notes.
- **permit** — jurisdiction_id, record_number (unique per jurisdiction),
  record_type, record_subtype, description, status, applied_date, issued_date,
  finaled_date, valuation, square_footage, occupancy_type, address fields,
  parcel_id, lat/lon, portal_url.
- **party** — permit_id, role (`applicant` | `owner` | `contractor` |
  `engineer` | `architect` | `agent`), raw_name, company_id (nullable FK),
  license_number, phone, email, address.
- **review** — permit_id, cycle_number, department, reviewer_name, status,
  status_date, due_date, comment_text. Departments to watch: anything matching
  `fire`, `life safety`, `fire marshal`, `FPB`.
- **document** — permit_id, title, portal_url, local_path, sha256, page_count,
  downloaded_at.
- **seal** — document_id, page_number, engineer_name, license_number,
  discipline, firm_name, extraction_method (`digital_signature` | `pdf_text` |
  `ocr`), confidence, bbox, raw_text.
- **company** — canonical_name, normalized_key, aliases (JSON), license numbers
  (JSON), phone, website, address, `has_engineering_ca` (bool),
  `has_fp_pe_on_record` (bool), `is_fp_contractor` (bool).
- **person** — name, license_number, license_type, discipline, status,
  company_id.
- **lead** — company_id, permit_id (nullable), score, score_breakdown (JSON),
  stage, next_action, notes, snoozed_until.

Migrations: keep it simple, use `alembic` only if the schema churns.

## 6. Module specifications

### Module 1 — Permit harvester

Implement a `JurisdictionAdapter` ABC with:

```python
def search(self, since: date, until: date, record_types: list[str]) -> Iterator[PermitStub]
def fetch_detail(self, stub: PermitStub) -> PermitDetail   # parties, reviews, documents
def list_documents(self, permit: PermitDetail) -> list[DocumentRef]
```

Platform notes you must verify, not assume:

- **Accela Citizen Access** — ASP.NET WebForms. Search driven by
  `__VIEWSTATE`/`__EVENTVALIDATION` postbacks, typically under
  `/Cap/CapHome.aspx` with detail at `/Cap/CapDetail.aspx`. Check whether the
  agency also exposes the Accela Construct API (`apis.accela.com`).
- **Tyler EnerGov / Citizen Self Service** — often has an undocumented but
  stable JSON search endpoint under `/api/energov/search/search`.
- **CentralSquare eTRAKiT** — WebForms, usually `/etrakit/Search/permit.aspx`.
- **Harris CityView Portal** — endpoints under `/Portal/`, often JSON.
- **ArcGIS / open data** — several Florida counties publish permits as a
  FeatureServer layer. The jackpot when it exists.

Filtering: commercial work only. Include new construction, shell, tenant
improvement/build-out, alteration, addition, change of occupancy, and any record
whose type or description matches
`sprinkler|fire alarm|fire suppression|standpipe|hood|clean agent|FACP|NFPA`.
Exclude single-family residential, roofing, mechanical-only replacements, pools,
fences, signs, and re-roofs.

Incremental crawling: store a per-jurisdiction watermark. Daily run pulls the
last 7 days plus re-checks any open permit touched in the last 90 days (for
Module 2). Full backfill is a separate, explicit CLI command with a date range.

Acceptance: `eversafe-leads harvest --jurisdiction orange --since 2026-06-01`
writes real permits with at least record number, type, status, address, and
applied date. A test with a saved fixture parses to the expected model.

### Module 2 — Fire review monitor

A filter and diff layer over Module 1, not a separate crawler.

- On each re-check of an open permit, compare new `review` rows against stored.
- Fire a HOT signal when a review row whose department matches the fire pattern
  transitions into a status matching
  `reject|denied|corrections|comments|revise|resubmit|disapprov|incomplete|not approved`.
- Also fire on: a permit sitting in fire review beyond normal turnaround, and a
  second or later review cycle on the same fire discipline.
- Capture reviewer comment text verbatim.
- Deduplicate: one alert per permit per review cycle, ever.

Acceptance: given two snapshots of the same permit as fixtures, the differ
produces exactly one HOT signal with the correct cycle number and comment text.

### Module 3 — Capability gap analysis

Build three licensing collectors. Verify current URLs and search mechanics.

- `sfm.py` — Florida Division of State Fire Marshal licensee search. Fire
  protection contractor licenses under F.S. Chapter 633 (Contractor I–V) and
  business entities. Capture license number, class, status, business name,
  address, phone, qualifier name.
- `fbpe.py` — Florida Board of Professional Engineers. Individual PE licensees
  (with discipline) and Certificates of Authorization held by business entities.
- `dbpr.py` — DBPR licensee data for construction contractors and architects.
  DBPR publishes downloadable licensee files; check for those first.

Analysis:

- Normalize every company name: uppercase, strip punctuation, strip
  `INC|LLC|L.L.C.|CORP|CO|COMPANY|PA|P.A.|LTD|GROUP|SERVICES|OF FLORIDA`,
  collapse whitespace. Keep the original.
- Resolve entities across sources: exact license number match, exact normalized
  name match, `rapidfuzz.token_set_ratio >= 92` combined with matching address
  or phone. 85–92 goes to a `review_queue` table — do not auto-merge.
- Compute per company: `is_fp_contractor`, `has_engineering_ca`,
  `has_fp_pe_on_record`, `permit_volume_12mo`.
- Target list A: FP contractors with active licenses, no CA, no FP PE on record,
  non-zero recent permit volume.
- Target list B: engineering/architecture firms with a CA and recent commercial
  permit volume, but no FP-discipline PE on record.
- Sort both by recent permit volume, descending.

Acceptance: `eversafe-leads targets --list A --county orange --limit 50` emits a
CSV with company, phone, license, permit count, and inclusion reason.

### Module 4 — Seal index

For permits with downloadable approved drawing sets:

1. Download PDFs (respect rate limits, cap file size, skip >~150 MB, resume,
   dedupe by sha256).
2. Try the digital signature first (`pikepdf`/`pyhanko`, per F.A.C. 61G15-23).
3. Then the PDF text layer (`pdfplumber`, lower-right quadrant).
4. Then OCR (`pymupdf` at 300 DPI, crop right 25% / bottom 30%, rotations
   0/90/180/270, keep highest mean confidence).
5. Regex extraction: `PE\s*#?\s*\d{4,6}`, `LICENSE\s*(NO\.?|#)\s*\d{4,6}`,
   `FL\s*PE\s*\d{4,6}`, CA numbers
   `(CA|C\.A\.|CERTIFICATE OF AUTHORIZATION)\s*#?\s*\d{3,6}`. Capture 200 chars
   of context into `raw_text`.
6. Validate every extracted license against FBPE data from Module 3.
7. Confidence per method: digital signature 0.98, PDF text 0.85, OCR 0.60
   baseline adjusted by OCR confidence.

Intelligence output — which engineers seal the most FP work per county; the
engineer→client relationship map; contractors relying on exactly one engineer;
sealing engineers with out-of-state addresses.

Acceptance: `eversafe-leads seals --county seminole --since 2025-01-01` produces
a table of engineer, PE number, seal count, distinct clients, and counties.

## 7. Lead scoring

Weights live in `config/scoring.yaml`, editable without code. Every score
carries a `score_breakdown` JSON explaining itself.

Starting weights:

- Fire review rejection in last 14 days: +40; second+ fire review cycle: +15.
- Company on Target List A: +30.
- Company on Target List B: +20.
- Occupancy/description in high-value niche (data center, battery/ESS,
  warehouse >100k sf, assisted living, high-rise, clean agent): +25.
- Valuation over $2M: +15; $500k–$2M: +8.
- Contractor seals through a single overloaded engineer (>40 seals/yr): +10.
- Permit applied within 30 days: +10, decaying linearly to 0 at 180 days.
- Already contacted in last 30 days: −50.

## 8. Outputs

- `eversafe-leads digest` — daily Markdown/HTML brief: HOT fire-review signals
  first with reviewer comment quoted and a suggested opening line, then new
  high-score permits, then new Target List A/B entries. Write to
  `data/digests/YYYY-MM-DD.md`.
- `eversafe-leads export --format csv` — flat file for CRM import.
- `eversafe-leads company <name>` — full dossier.
- Scheduling: provide a `cron` line and a `launchd`/systemd unit in the README.
  No hosted infrastructure in v1.

## 9. Build phases — do not skip the gates

- **Phase 0 — Reconnaissance.** Write no scraper code. For each jurisdiction,
  determine and report a table: portal platform/version, base URL, API/bulk/
  ArcGIS existence, login requirement, review-status visibility, document
  downloadability, robots.txt and TOU, observed rate-limit behavior. Confirm
  search mechanics for the three licensing sources. Stop and show the table.
- **Phase 1** — Scaffold repo, models, DB, CLI skeleton, config, `.gitignore`,
  one adapter for the easiest jurisdiction. One command writes 20 real rows.
- **Phase 2** — Remaining five adapters, incremental crawl, fixture tests.
- **Phase 3** — Module 2 review differ and HOT signals.
- **Phase 4** — Module 3 licensing collectors, normalization, resolution,
  target lists.
- **Phase 5** — Module 4 seals, digital signature path first, OCR last.
- **Phase 6** — Scoring, digest, exports, scheduling docs.

End of every phase: run tests, run `ruff`, commit, record every portal quirk in
`CLAUDE.md`.

## 10. Before you start

Do reconnaissance first. Do not ask which portal platform each county uses — go
look.
