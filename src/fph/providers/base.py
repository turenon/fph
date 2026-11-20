from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from fph.config import RegionConfig
from fph.models import RawAuctionDetail, RawAuctionItem


class ProviderError(RuntimeError):
    """General provider execution failure."""


class AuthRequiredError(ProviderError):
    """Raised when login or human verification is required in the local browser."""

    def __init__(self, message: str, url: str = "", task_space: str = "") -> None:
        super().__init__(message)
        self.url = url
        self.task_space = task_space


@dataclass(slots=True)
class PageResult:
    items: list[RawAuctionItem] = field(default_factory=list)
    page: int = 1
    total_pages: int | None = None
    has_next: bool = False
    source_url: str = ""
    extractor: str = ""


class AuctionProvider(ABC):
    """Pluggable interface for auction data sources (Taobao ego-lite, JD, etc.)."""

    platform: str = "unknown"

    @abstractmethod
    def check_ready(self) -> tuple[bool, str]:
        """Verify runtime prerequisites without scraping."""

    @abstractmethod
    def fetch_list_page(
        self,
        region: RegionConfig,
        status_scope: str,
        page: int,
    ) -> PageResult:
        """Fetch a single paginated list page for a region & status scope."""

    @abstractmethod
    def fetch_detail(self, item_id: str, url: str) -> RawAuctionDetail:
        """Fetch detail & announcement content for a single auction item."""
