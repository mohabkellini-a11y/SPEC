"""JurisdictionAdapter ABC (SPEC §6, Module 1)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import date
from typing import Any

from ..models import DocumentRef, PermitDetail, PermitStub


class JurisdictionAdapter(ABC):
    """One adapter per portal platform.

    Concrete subclasses set ``platform`` and are constructed with the
    jurisdiction's config block (from ``config/jurisdictions.yaml``).
    """

    platform: str = ""

    def __init__(self, slug: str, config: dict[str, Any]) -> None:
        self.slug = slug
        self.config = config
        self.base_url: str = config["base_url"]

    @abstractmethod
    def search(
        self, since: date, until: date, record_types: list[str] | None = None
    ) -> Iterator[PermitStub]:
        """Yield permit stubs for the date window (commercial filter applied)."""

    @abstractmethod
    def fetch_detail(self, stub: PermitStub) -> PermitDetail:
        """Return a fully-populated permit: parties, reviews, documents."""

    def list_documents(self, permit: PermitDetail) -> list[DocumentRef]:
        """Documents for a permit. Default: whatever detail already gathered."""
        return permit.documents
