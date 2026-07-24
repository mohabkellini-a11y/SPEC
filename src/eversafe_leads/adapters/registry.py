"""Map a jurisdiction's ``platform`` string to its adapter class.

Adding a jurisdiction that reuses an existing platform is a config edit only.
Adding a new platform is one entry here plus one adapter class (SPEC §1).
"""

from __future__ import annotations

from typing import Any

from .arcgis import ArcgisAdapter
from .base import JurisdictionAdapter
from .lake_reports import LakeReportsAdapter
from .socrata import SocrataAdapter

_REGISTRY: dict[str, type[JurisdictionAdapter]] = {
    SocrataAdapter.platform: SocrataAdapter,
    LakeReportsAdapter.platform: LakeReportsAdapter,
    ArcgisAdapter.platform: ArcgisAdapter,
}


class AdapterNotImplemented(RuntimeError):
    """Raised for a jurisdiction whose platform has no adapter yet."""


def get_adapter(slug: str, config: dict[str, Any]) -> JurisdictionAdapter:
    platform = config.get("platform")
    cls = _REGISTRY.get(platform)
    if cls is None:
        raise AdapterNotImplemented(
            f"no adapter for platform '{platform}' (jurisdiction '{slug}'). "
            f"available: {sorted(_REGISTRY)}"
        )
    return cls(slug, config)
