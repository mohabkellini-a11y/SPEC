# CLAUDE.md — working notes, portal quirks, gotchas

The real asset in this repo. Record every selector that broke, every required
header, every date-format inconsistency. Append; don't overwrite.

## Environment gotchas

- **2026-07-22 (UPDATE) — Egress is now OPEN.** The earlier CONNECT-denial
  blocker is resolved: `curl -sS "$HTTPS_PROXY/__agentproxy/status"` reports
  `selective:false`, no relay failures, and live requests to
  `data.cityoforlando.net`, `services.arcgis.com`, `fbpe.org`,
  `fasttrack.ocfl.net`, `semc-egov.aspgov.com`, `connectlivepermits.org`,
  `c.lakecountyfl.gov`, `licenseesearch.fldfs.com`, `citizenserve.com` all
  return 200. Phase 0 re-run and endpoint verification are done — see
  `docs/PHASE0_RECON.md`.
- **Osceola cert quirk (still blocking that ONE host).** `permits.osceola.org`
  (Accela) re-signs through an *"Egress Gateway SDS Issuing CA (production)"*
  that is **not** in `/root/.ccr/ca-bundle.crt`. curl and Python
  (`SSL_CERT_FILE`=bundle) both fail with `unable to get local issuer
  certificate`. The TLS tunnel connects; only chain verification fails. Before
  building the Osceola adapter, either get that CA added to the bundle or run
  the adapter from the practice's own box. Do **not** silently disable TLS
  verification — flag it.
- **(historical) The pre-2026-07-22 egress was a restrictive allow-list** that
  403'd every target host at CONNECT. Kept here only as history; no longer true.

## Portal quirks (verified against live endpoints 2026-07-22)

- **Orlando — Socrata ✅ VERIFIED (jackpot).** `data.cityoforlando.net`,
  dataset `ryhf-m453`. SODA API `/resource/ryhf-m453.json` with
  `$where/$select/$group/$order/$limit`. Date filter column is **`processed_date`**
  (there is NO `application_date` — it 400s). Fire work is native in `worktype`:
  `FireSupp` and `FA`; fire permit numbers prefixed `FIR####`. `plan_review_type`
  is `Commercial` / `Residential 1/2` / `Residential 3 or more` / `No Plan Review
  Type` — filter commercial on `plan_review_type = 'Commercial'`. robots
  `Crawl-delay: 1`; API path not disallowed. Review comment text is NOT in this
  dataset — only `of_cycles` + `under_review_date` (Module 2 gets cycle-count
  diffs, not comment quotes, from Socrata alone).
  - **WAF encoding trap (cost an hour — READ THIS).** A CloudFront/WAF fronts
    the SODA API and 403s a complex `$where` unless the query string is encoded
    two specific ways at once: (1) **spaces as `%20`, never `+`** — a `+`
    between quoted literals in `A AND B AND C` reads as SQL-injection and is
    blocked; (2) **the `$` in `$where`/`$order`/`$limit` stays literal** — if
    it's percent-encoded to `%24where` (which `urllib.parse.urlencode` does by
    default) the request 403s. `httpx`'s `params=` dict uses `quote_plus`
    (spaces→`+`) and trips (1). Fix: build the query string by hand with
    `urllib.parse.quote(value, safe='')` and literal `$` keys (see
    `adapters/socrata.py::_build_url`). Simple single-clause queries pass either
    way, which is why `curl` (using `%20`) worked but httpx didn't.
- **Orange — ⚠️ old ArcGIS org was WRONG.** `services.arcgis.com/v400IkDOw1ad7Yad`
  is **Raleigh, NC**, not Orange FL (sample rows say `contractorstate:"NC"`).
  Do not use it. Orange's own `ocgis4.ocfl.net` has no public permits layer.
  Fast Track (`fasttrack.ocfl.net`) robots.txt **`Disallow: /`** except the
  landing page → scraping is robots-forbidden. **Route Orange to a Ch. 119
  records request / `EPlanCom@ocfl.net`.**
- **Osceola — Accela** `permits.osceola.org/CitizenAccess/Cap/CapHome.aspx`
  (WebForms `__VIEWSTATE` postbacks). Reachable but cert-verification blocked in
  this env (see gotcha above). robots unread.
- **Seminole — Click2Gov BP** `semc-egov.aspgov.com/Click2GovBP/index.html`
  (200, live). No robots.txt (404) → apply SPEC default 1/2s.
- **Lake — ✅ VERIFIED, custom report pages.** `c.lakecountyfl.gov/offices/
  building_services/permit_activity_reports/permits_issued.aspx`. ASP.NET
  WebForms: GET the page for `__VIEWSTATE`/`__VIEWSTATEGENERATOR`/
  `__EVENTVALIDATION`, then POST `lbPermitTypes=All`, `lbCities=All`,
  `txtStartDate`/`txtEndDate` (**MM/DD/YYYY**), `rblDetails=2`, `btnSubmit=Search
  Now`. `rblDetails=1` is a summary; **`2` gives individual permits**. Cookies
  from the GET must persist to the POST (httpx.Client does this). Response groups
  permits under a type header, then two cells each: `"2026031318 ISSUED"` and
  `"<addr> / <CITY> <desc>"`. Fire is native: `FSC` (sprinkler comm), `FALC`
  (alarm comm), `FMC` (fire main), `FE` (suppression). **Quirks:** rows carry NO
  per-permit date (window is the query) → `issued_date` left null; multi-word
  cities (`Grand Island`, `Mount Dora`, `Howey-In-The-Hills`) split on the first
  token so `city` is approximate and the rest leaks into `description` — full
  string preserved in `raw_payload`. robots does NOT block `building_services/`.
  Multi-value `lbPermitTypes` via httpx list-form 500s through the proxy — use
  `All` and filter client-side (adapter does this).
- **Volusia — Accela Civic Access** `connectlivepermits.org/citizenportal/`.
  robots `Allow: /` (blocks only `/publicportal/`, `/citizenportal/integration/`)
  and explicitly allows `/citizenportal/app/public-search`. That literal path
  404s on a bare GET (JS app) — find the underlying `/citizenportal/rest/...`
  Civic Access call when building.

## Licensing source quirks (verified 2026-07-22)

- **SFM** — F.S. 633 fire-protection contractor licenses (Contractor I–V) live
  in the SFM **CitizenServe** portal `citizenserve.com/120/`. Verified: it's a
  heavy JS app — `showSearchLicensePage&installationID=120` returns a 137 KB page
  whose license search renders via XHR; the static HTML exposes only
  permit/complaint AJAX actions (`getPermitDetail`, `listInspections`…), **no
  scrapeable license-search endpoint or result schema**. So `licensing/sfm.py`
  imports a roster CSV (records request / portal export), resolves firms, and
  sets `is_fp_contractor` — the flag Target List A needs. **Do NOT** use DFS
  `licenseesearch.fldfs.com` "Industrial Fire & Burglary" for this — that
  category is alarm/insurance agents, not Ch. 633 system contractors.
- **FBPE** — `fbpe.org/licensure/licensee-search/` (200). robots
  **`Crawl-delay: 30`** — one request / 30 s, the binding constraint on Module 3.
  The on-page search **delegates to `myfloridalicense.com/wl11.asp?mode=0`** (an
  opaque classic-ASP search), and the "engineering directory" download links
  (`fbpe.org/download/39064/`, `/39067/`) resolve to **HTML landing pages, not
  data files**. So there is no clean bulk endpoint — SPEC-preferred path is the
  **directory / Ch. 119 records request** for a PE + CA roster. `licensing/fbpe.py`
  imports those rosters (CSV) rather than scraping. The CA roster is the key: it
  links firm → qualifying PE → discipline, which sets `has_engineering_ca` and
  `has_fp_pe_on_record` — the two flags Target List B needs. Any future live
  per-name lookup MUST honor the 30 s crawl-delay (not faked in code).
- **DBPR** — ✅ VERIFIED bulk CSV, strongest cheap path. Index
  `www2.myfloridalicense.com/construction-industry/public-records/`; construction
  extract `…/sto/file_download/extracts/CONSTRUCTIONLICENSE_1.csv` is live (48 MB,
  text/csv, last-modified today → ~daily refresh). **No header row; positional
  columns** — full license like `CBC015061`. Confirm DBPR's published field
  layout before trusting column positions. Siblings: `constr_app.csv`,
  `cilb_certified.csv`, `cilb_registered.csv`. Note the URL uses literal
  underscores (`file_download`), not the `%5F` some pages render.
- **Volusia adapter note** — `connectlivepermits.org/citizenportal/` → 302 →
  `/citizenportal/app` (Accela Civic Access Angular SPA). Public-search REST
  route is NOT at `/citizenportal/rest/*` or `/app/rest/*` (all 404); capture it
  from the SPA's XHR with Playwright before building — don't guess.

### DBPR construction extract — parsing quirks (verified against 15k real rows)

- **Headerless positional CSV.** Column map lives in `licensing/dbpr.py` (COL_*).
  Verified across 15,485 live rows: 99.9% pass the license-reconstruction guard.
- **`full_license` (col 20) is authoritative**, not `type+number`. DBPR inserts
  an `A` designation and re-encodes the numeric part: type `CBC` + numeric
  `1114582` → `CBCA14582` (the `14582` is a substring of `1114582`). Use col 20
  as the canonical license; the guard tolerates this, only flagging real shifts.
- **`INDIVIDUAL` in the DBA column is a sentinel, not a business name.** Sole
  practitioners carry DBA=`INDIVIDUAL`; treating it as a company collapses
  hundreds of unrelated licenses into one bogus "INDIVIDUAL" company. Also seen:
  `SOLE PROPRIETOR`, `N/A`, `NONE`. Filtered in `_DBA_SENTINELS`.
- Two status-code columns (13, 14, e.g. `C`/`I`) — semantics NOT confirmed
  against DBPR's layout doc; stored raw as `"C/I"`, not interpreted.
- Encoding is latin-1, not utf-8. `text/csv`, ~48 MB, last-modified daily.
