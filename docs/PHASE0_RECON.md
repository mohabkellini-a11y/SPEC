# Phase 0 — Reconnaissance Findings

**Date:** 2026-07-22
**Analyst:** Claude Code (remote web session)

---

## 0. Blocking finding: this environment cannot reach the target portals

The SPEC's Phase 0 requires verifying every portal with **real HTTP requests**
(read robots.txt, probe endpoints, observe rate-limit behavior). I attempted
this. The result is a hard, honest blocker:

**This remote session's outbound network egress policy is a restrictive
allow-list that denies every target host.** Package registries (`pypi.org`,
`github.com`) tunnel fine, but every government / open-data host I tried is
rejected by the egress proxy with `403` on the `CONNECT` tunnel — a policy
denial, before any request reaches the destination.

Confirmed denials recorded by the proxy (`connect_rejected`,
"gateway answered 403 to CONNECT (policy denial)"):

| Host | Result |
|------|--------|
| `services.arcgis.com` (Orange Co. permits FeatureServer) | 403 CONNECT — blocked |
| `data.cityoforlando.net` (Orlando Socrata API) | 403 CONNECT — blocked |
| `fasttrack.ocfl.net` (Orange Co. Fast Track portal) | 403 CONNECT — blocked |
| `fbpe.org` (FL Board of Professional Engineers) | 403 CONNECT — blocked |

`WebFetch` (which also tunnels through this proxy) returns HTTP 403 for the same
hosts. The **only** tool that reaches the outside world is `WebSearch`, which
routes through an allowlisted search backend.

**Consequences:**

1. I could **not** directly read any portal's `robots.txt`, terms of use, or
   observe rate-limit behavior — all four are policy-denied at CONNECT.
2. I could **not** confirm API field schemas, ASP.NET postback flows, or JSON
   endpoint shapes by hitting them.
3. Any scraper written and "run" from this environment would fail at the network
   layer, not the code layer — exactly the silent-zero-rows failure the SPEC
   forbids.

Everything in the tables below is therefore **search-derived** (from result
snippets returned by `WebSearch`), **not endpoint-verified**. It is high-signal
for planning — it identifies platforms and confirms which cheap-path APIs exist —
but each row must be re-verified against the live endpoint from a network
location that is allowed to reach these hosts before any parser is trusted.

**To unblock:** the environment needs a network policy that permits outbound
HTTPS to these hosts, or the harvester must run from the practice's own
machine / a box with open egress. See "Recommendation" at the bottom.

---

## 1. Jurisdiction portals (search-derived; not endpoint-verified)

| Jurisdiction | Portal / platform (likely) | Entry URL | Cheap-path API / bulk? | Notes to verify |
|---|---|---|---|---|
| **City of Orlando** | Socrata open data + "WebPermits" lookup | `data.cityoforlando.net` (dataset `ryhf-m453` "Permit Applications"); `permitlookup.cityoforlando.net/WebPermits/` | **YES — Socrata SODA API** (documented, queryable JSON, `$where`/`$limit`); also ArcGIS Hub `orlando-open-data-orl.hub.arcgis.com` | Socrata is the jackpot: no scraping, documented API. Verify the dataset carries review/fire-review status + valuation. |
| **Orange County** | Custom "Fast Track" ASP.NET WebForms | `fasttrack.ocfl.net/OnlineServices/` (`PermitsAllTypes.aspx`) | **YES — ArcGIS FeatureServer** `services.arcgis.com/v400IkDOw1ad7Yad/.../Building_Permits_Pending/FeatureServer/0`; open data hub `data-ocpw.opendata.arcgis.com` | Layer is named "Pending" — confirm an *issued* layer also exists. Fast Track portal itself is WebForms (needs Playwright) if detail/review data isn't in the FeatureServer. |
| **Osceola County** | **Accela** Citizen Access | `permits.osceola.org/CitizenAccess/Cap/CapHome.aspx` | Check Accela Construct API (`apis.accela.com`) for this agency | Classic Accela `__VIEWSTATE` postback flow. Verify whether review cycles/comments are public on `CapDetail.aspx`. |
| **Seminole County** | **CentralSquare Click2Gov** (BP) | `semc-egov.aspgov.com/Click2GovBP/index.html` | Unknown — check for bulk/records extract | Click2Gov, not eTRAKiT. Verify date-range search + whether plan-review status is exposed. |
| **Lake County** | **Accela** Citizen Access + custom report pages | Accela portal (unconfirmed base); `c.lakecountyfl.gov/offices/building_services/permit_activity_reports/*.aspx`; `mcdplus.lakecountyfl.gov/oprs_PT/` (OPRS) | Custom "permit activity reports" pages may be scrape-friendly grids | Two systems coexist. Find the canonical Accela base URL; confirm which surface has review data. |
| **Volusia County** | **Accela** Citizen Access + "Connect Live" | `connectlivepermits.org`; Accela portal (unconfirmed base) | Unknown — check ArcGIS/open data + records request | Two front-ends. Verify which is authoritative for commercial permits + review status. |

### Cheap-path ranking (SPEC §2 order)

1. **Documented API:** Orlando (Socrata SODA) — strongest. Orange (ArcGIS
   FeatureServer REST) — strong.
2. **Bulk / GIS:** Orange County ArcGIS hub; Orlando ArcGIS hub.
3. **Records request (Ch. 119, "ask, don't scrape"):** Seminole, Lake, Volusia,
   Osceola all worth a records request for a commercial-permit extract before
   committing to WebForms scraping — several FL counties hand these over.
4. **HTML/WebForms scraping (last resort):** Osceola/Lake/Volusia Accela;
   Seminole Click2Gov; Orange Fast Track — only if the API/records paths don't
   carry review-cycle data.

**Easiest Phase 1 jurisdiction (once network is unblocked): City of Orlando**
via the Socrata API — documented, paginated, no browser, no postbacks. Orange
County ArcGIS is a close second.

---

## 2. Licensing sources for Module 3 (search-derived)

| Source | What it covers | Access mechanism (likely) | Cheap path? |
|---|---|---|---|
| **State Fire Marshal — Bureau of Fire Prevention** (`sfm.py`) | FP contractor licenses (F.S. 633), business entities, qualifiers | **CitizenServe portal** `citizenserve.com/120/CAPFor120?Action=SearchLicenses` | Search form; verify export/records-request option |
| **FL Board of Professional Engineers** (`fbpe.py`) | Individual PEs (with discipline) + Certificates of Authorization | Licensee search `fbpe.org/licensure/licensee-search/`; directory `fbpe.org/meetings-info/engineering-directory/`; public-records form `fbpe.org/legal/public-records/` | **Directory download likely**; else Ch. 119 records request. (`fbpe.org` is egress-blocked here — unverified.) |
| **DBPR** (`dbpr.py`) | Construction contractors, architects | **Weekly bulk CSV/ASCII downloads** via `myfloridalicense.com` public records; live search `wl11.asp` | **YES — documented weekly bulk download** (Current/Active/Inactive; excludes NULL & VOID). Strongest cheap path of the three. |

Bonus: DFS runs a bulk-download licensee search at
`licenseesearch.fldfs.com/BulkDownload` — verify whether FP-related licenses live
there vs. the SFM CitizenServe portal.

---

## 3. Robots.txt / terms of use

**Not retrievable from this environment** — every target host is egress-blocked
at CONNECT, so I could not read a single `robots.txt` or TOU page. This must be
done from a network location allowed to reach the hosts, before any scraper runs.
Recording this as an explicit open item rather than guessing.

---

## 4. Recommendation / decision needed

The build cannot proceed past planning from *this* environment because the
network policy denies the target hosts. Options for the operator:

- **A. Re-provision the web environment with an open (or portal-allowlisted)
  network policy**, then I re-run true Phase 0 (real robots.txt + endpoint
  probes) and proceed through the phases here.
- **B. Build the code now against the search-derived design** (models, config,
  CLI skeleton, adapters coded to the documented Socrata/ArcGIS/Accela shapes)
  with fixture-backed tests, accepting that live verification and the first real
  `harvest` run happen later on a machine with open egress (the practice's own
  box). No live rows can be shown from here.
- **C. Lead with records requests** (Ch. 119) to Seminole/Lake/Volusia/Osceola
  and DBPR/FBPE bulk downloads — the "ask, don't scrape" path — which sidesteps
  both the scraping and the egress problem for a large share of the data.

My recommendation: **A** if you can change the environment's network policy
(cleanest — lets me honor the SPEC's "verify, don't assume" mandate here), else
**B** to make real progress on code now, with a clear "unverified" marker on
every endpoint until it's been hit for real.
