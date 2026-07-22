# CLAUDE.md — working notes, portal quirks, gotchas

The real asset in this repo. Record every selector that broke, every required
header, every date-format inconsistency. Append; don't overwrite.

## Environment gotchas

- **2026-07-22 — Remote web session egress is a restrictive allow-list.**
  Outbound HTTPS goes through an agent proxy whose upstream gateway denies the
  target hosts at the `CONNECT` tunnel (`403`, `connect_rejected`, "policy
  denial"). Confirmed blocked: `services.arcgis.com`, `data.cityoforlando.net`,
  `fasttrack.ocfl.net`, `fbpe.org`. Allowed: `pypi.org`, `github.com`. Only
  `WebSearch` reaches the outside (allowlisted search backend); `WebFetch` and
  `curl` both 403 on the target hosts. Diagnose with
  `curl -sS "$HTTPS_PROXY/__agentproxy/status"`. **Implication:** no scraper can
  actually run from this environment; Phase 0 endpoint verification and the first
  live `harvest` must happen from a machine with open egress. See
  `docs/PHASE0_RECON.md` §0.

## Portal quirks (fill in as verified against live endpoints)

- Orlando — Socrata (`data.cityoforlando.net`, dataset `ryhf-m453`): _unverified_
- Orange — ArcGIS FeatureServer (`services.arcgis.com/v400IkDOw1ad7Yad/...`),
  Fast Track WebForms (`fasttrack.ocfl.net`): _unverified_
- Osceola — Accela (`permits.osceola.org/CitizenAccess/`): _unverified_
- Seminole — Click2Gov (`semc-egov.aspgov.com/Click2GovBP/`): _unverified_
- Lake — Accela + custom report pages (`c.lakecountyfl.gov`): _unverified_
- Volusia — Accela + Connect Live (`connectlivepermits.org`): _unverified_

## Licensing source quirks (fill in as verified)

- SFM — CitizenServe (`citizenserve.com/120/`): _unverified_
- FBPE — licensee search + engineering directory (`fbpe.org`): _unverified_
- DBPR — weekly bulk CSV downloads (`myfloridalicense.com`): _unverified_
