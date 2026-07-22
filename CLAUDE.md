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
- **Lake — custom report pages** `c.lakecountyfl.gov/offices/building_services/
  permit_activity_reports/` (302 to index). robots does NOT block
  `building_services/`. Scrape-friendly grids; Accela is secondary.
- **Volusia — Accela Civic Access** `connectlivepermits.org/citizenportal/`.
  robots `Allow: /` (blocks only `/publicportal/`, `/citizenportal/integration/`)
  and explicitly allows `/citizenportal/app/public-search`. That literal path
  404s on a bare GET (JS app) — find the underlying `/citizenportal/rest/...`
  Civic Access call when building.

## Licensing source quirks (verified 2026-07-22)

- **SFM** — `citizenserve.com/120/` (200) + DFS `licenseesearch.fldfs.com` (200)
  which has a **Bulk Downloads** section and category *"Industrial Fire &
  Burglary"*. Bulk page is a thin JS shell; file endpoint is behind a form POST.
- **FBPE** — `fbpe.org/licensure/licensee-search/` (200). robots
  **`Crawl-delay: 30`** — one request / 30 s, the binding constraint on Module 3.
  Prefer directory / Ch. 119 records over per-name scraping.
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
