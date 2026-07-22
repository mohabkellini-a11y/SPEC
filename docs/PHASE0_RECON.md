# Phase 0 — Reconnaissance Findings (VERIFIED)

**Date:** 2026-07-22 (re-run with open egress)
**Analyst:** Claude Code (remote web session)
**Status:** Endpoints hit live. Every row below is endpoint-verified unless it
says otherwise. Supersedes the earlier search-derived draft.

---

## 0. Egress: now open

The earlier run was blocked — the session's egress proxy denied every target
host at `CONNECT`. That is **resolved**. `curl -sS "$HTTPS_PROXY/__agentproxy/status"`
now reports `selective: false` and no recent relay failures, and live requests
succeed:

| Host | Result |
|------|--------|
| `data.cityoforlando.net` (Orlando Socrata) | 200 — live JSON |
| `services.arcgis.com` (ArcGIS REST) | 200 |
| `fbpe.org` | 200 (robots + pages) |
| `fasttrack.ocfl.net` | 200 |
| `semc-egov.aspgov.com`, `connectlivepermits.org`, `c.lakecountyfl.gov`, `licenseesearch.fldfs.com`, `citizenserve.com` | 200 |

One host-specific quirk remains: **`permits.osceola.org` (Osceola Accela)**. The
TLS tunnel connects, but the proxy re-signs its cert with an *"Egress Gateway SDS
Issuing CA (production)"* that is **not** in `/root/.ccr/ca-bundle.crt` (the bundle
carries the other Egress Gateway / TLS Inspection CAs, not the SDS issuing one).
So curl **and** Python (`SSL_CERT_FILE` pre-set to the bundle) both fail cert
verification for this one host with `unable to get local issuer certificate`.
The host is reachable; only chain verification fails. See CLAUDE.md for the
implication when the Osceola adapter is built.

---

## 1. Jurisdiction portals (endpoint-verified)

### City of Orlando — Socrata SODA API ✅ JACKPOT, verified

- Dataset **`ryhf-m453`** ("Permit Applications") is **live and current** —
  rows dated **2026-07-22** (today) returned.
- SODA API works: `https://data.cityoforlando.net/resource/ryhf-m453.json`
  with `$where`, `$select`, `$group`, `$order`, `$limit`. No key needed for
  light use (app token recommended for volume).
- **robots.txt:** `Crawl-delay: 1`; disallows some `/browse?...` query facets
  only. The `/resource/` API path is not disallowed. Respect crawl-delay ≥ 1s.
- **Real fields present:** `permit_number`, `application_type`, `worktype`,
  `permit_address`, `property_owner_name`, `parcel_owner_name`, `contractor`,
  `contractor_name`, `contractor_address`, `contractor_phone_number`,
  `plan_review_type`, `estimated_cost`, `square_footage`, `application_status`,
  `processed_date`, `under_review_date`, `prescreen_completed_date`,
  `of_cycles`, `issue_permit_date`, `final_date`, `coo_date`, `project_name`,
  `location`, `geocoded_column` (Point), `private_provider*`.
- **Fire signal is native:** `worktype` has dedicated values `FireSupp`
  (29,593 rows all-time) and `FA` (fire alarm, 20,219). 259 `FireSupp`/`FA`
  permits since 2026-06-01. Fire permit numbers are prefixed `FIR####`.
- **Date column for filtering is `processed_date`** — there is **no**
  `application_date` column (the earlier draft assumed one; it 400s).
- **Module 2 caveat:** the dataset is a permit-level snapshot. It exposes
  `of_cycles` (review-cycle count) and `under_review_date`, but **no per-cycle
  reviewer, status, or comment text.** The differ can flag "cycle count went
  from 1→2" but cannot quote a fire-review comment from Socrata alone. Comment
  text would need the WebPermits detail page (`permitlookup.cityoforlando.net`)
  — flag as a Phase 3 follow-up.

### Orange County — earlier ArcGIS org was WRONG; Fast Track is robots-blocked ⚠️

- **Correction:** the org `services.arcgis.com/v400IkDOw1ad7Yad/...` cited in
  the earlier draft is **Raleigh, North Carolina**, not Orange County FL. Its
  `Building_Permits/FeatureServer/0` sample rows carry `contractorcity:"RALEIGH",
  contractorstate:"NC"`. Do not use it.
- Orange County's own GIS server `ocgis4.ocfl.net/arcgis/rest/services` exposes
  base/aerial/address layers but **no public building-permits feature layer**
  (Public_Dynamic has only LEED / green-building layers). No Orange County FL
  permits FeatureServer was found via ArcGIS Hub search either.
- **Fast Track** (`fasttrack.ocfl.net/OnlineServices/`) is a live ASP.NET
  WebForms portal (`PermitsAllTypes.aspx`, `permit-building.aspx?SearchID=COM`),
  but its **robots.txt disallows `/`** for all agents except
  `/OnlineServices/Default.aspx`. **Scraping the search pages violates robots**
  — flag as a TOU conflict; do not scrape without a decision.
- **Cheap-path verdict for Orange:** downgraded from "strong API" to **records
  request (Ch. 119)** or member-services/EPlan contact
  (`EPlanCom@ocfl.net`, 407-836-5550). No compliant automated path found yet.

### Osceola County — Accela Citizen Access (reachable, cert quirk) ⚠️

- `permits.osceola.org/CitizenAccess/Cap/CapHome.aspx` — classic Accela ACA
  WebForms (`__VIEWSTATE`/`__EVENTVALIDATION` postbacks).
- Reachable through the tunnel but **cert fails verification** in this env (SDS
  issuing CA not in bundle — see §0). Robots.txt could not be read for the same
  reason. Resolve the CA before building the adapter, or run it from the
  practice's box.

### Seminole County — CentralSquare Click2Gov BP (reachable) ✅

- `semc-egov.aspgov.com/Click2GovBP/index.html` — HTTP 200, live.
- **No robots.txt** (404) → no crawl directives; still apply the SPEC's default
  1 req / 2 s. Click2Gov, not eTRAKiT. Verify date-range search + whether
  plan-review status is exposed on the detail page when the adapter is built.

### Lake County — custom permit-activity report pages (reachable, robots-OK) ✅

- `c.lakecountyfl.gov/offices/building_services/permit_activity_reports/` —
  302 (redirect to the live report index). **robots.txt does NOT disallow**
  the `building_services/` path (it blocks `/bin/`, `/ems/`, `/Templates/`,
  etc.). These custom report grids are the scrape-friendly surface; the Accela
  side is secondary. Confirm grid columns + whether review data is present.

### Volusia County — Accela "Connect Live" citizen portal (reachable, robots-OK) ✅

- `connectlivepermits.org` — 200. This is a modern Accela Civic Access app
  under `/citizenportal/`.
- **robots.txt is explicit and permissive for search:** `User-agent: *`
  `Allow: /`, disallowing only `/publicportal/` and
  `/citizenportal/integration/`, and it **explicitly allows**
  `/citizenportal/app/public-search`. (That exact literal path returned 404 on a
  bare GET — the real app route is JS-driven; find the underlying Civic Access
  REST call, typically `/citizenportal/rest/...`, when building the adapter.)

### Cheap-path ranking (revised, SPEC §2 order)

1. **Documented API:** **Orlando (Socrata)** — verified, current, fire-native.
   The clear Phase 1 jurisdiction.
2. **Robots-permitted HTML/portal:** Lake (report pages), Volusia (Civic Access
   public-search), Seminole (Click2Gov, no robots).
3. **Records request (Ch. 119, "ask, don't scrape"):** **Orange County** (no
   compliant automated path — Fast Track robots-blocked, no public FeatureServer),
   plus Osceola pending the cert fix.
4. **WebForms scraping (last resort):** Osceola Accela.

---

## 2. Licensing sources for Module 3 (endpoint-verified reachability)

| Source | URL (live) | Access | Cheap path | robots |
|---|---|---|---|---|
| **State Fire Marshal** (`sfm.py`) | `citizenserve.com/120/` (200) + DFS `licenseesearch.fldfs.com` (200, has "Bulk Downloads" + category *"Industrial Fire & Burglary"*) | Search form / bulk | Check DFS bulk first, then CitizenServe | tbd |
| **FL Board of Prof. Engineers** (`fbpe.py`) | `fbpe.org/licensure/licensee-search/` (200) | Licensee search + directory | Directory / Ch. 119 records | **`Crawl-delay: 30`** — very slow, must honor |
| **DBPR** (`dbpr.py`) | `myfloridalicense.com` (302 → app) | Live search + weekly bulk file downloads | **Bulk CSV/ASCII — strongest** | tbd |

- **DFS `licenseesearch.fldfs.com`** confirms a **Bulk Downloads** section and a
  license category **"Industrial Fire & Burglary"** — the State Fire Marshal
  path. The bulk-download page itself is a thin JS shell (6 KB); the file
  endpoint is behind a form POST — resolve when building `sfm.py`.
- **FBPE** `Crawl-delay: 30` is the binding constraint on Module 3 — one request
  every 30 s. Prefer the directory/records-request path over per-name scraping.
- **DBPR** weekly bulk download remains the strongest single cheap path for
  contractors/architects; confirm the current download URL when building.

---

## 3. robots.txt / TOU summary (read live)

| Host | robots posture | Action |
|---|---|---|
| Orlando Socrata | Crawl-delay 1; API path allowed | OK — throttle ≥1s |
| Orange Fast Track | **Disallow `/`** (only landing page allowed) | **Do not scrape** — records request |
| Osceola Accela | not readable (cert) | resolve cert, then read |
| Seminole Click2Gov | no robots.txt (404) | apply SPEC default 1/2s |
| Lake report pages | `building_services/` allowed | OK |
| Volusia Civic Access | `public-search` explicitly allowed | OK |
| FBPE | **Crawl-delay 30** | one req / 30s |

No legal advice given; the Orange Fast Track `Disallow: /` is flagged as a
TOU/robots conflict for the operator to decide (SPEC §2).

---

## 4. Decision → proceeding with build

Egress is open, so the SPEC's "verify, don't assume" gate is satisfied for the
API/bulk paths. **Phase 1 proceeds now with City of Orlando (Socrata)** — the
one fully-verified, robots-clean, fire-native, currently-live source. Remaining
adapters follow in Phase 2 with the per-jurisdiction notes above; Orange County
is re-routed to a records request rather than a non-compliant scraper.
