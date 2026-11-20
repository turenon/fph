from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RawAuctionItem:
    """Normalized item returned by any pluggable auction provider (list page)."""

    platform: str
    item_id: str
    url: str
    title: str
    province: str = ""
    city: str = ""
    district: str = ""
    stage: str = ""  # 一拍 / 二拍 / 变卖 / 重新拍卖 / 未知
    status: str = "unknown"  # upcoming / ongoing / sold / failed / withdrawn / suspended / ended / unknown
    initial_price: float | None = None  # 起拍价（元）
    current_price: float | None = None  # 当前价/成交价（元）
    consult_price: float | None = None  # 评估价/市场价（元）
    deposit_price: float | None = None  # 保证金（元）
    bid_count: int | None = None  # 出价次数
    apply_count: int | None = None  # 报名人数
    watch_count: int | None = None  # 围观/提醒人数
    start_time: str | None = None  # ISO8601 local time string YYYY-MM-DD HH:MM:SS
    end_time: str | None = None  # ISO8601 local time string YYYY-MM-DD HH:MM:SS
    court_name: str = ""
    category_hint: str = ""
    raw_payload: dict[str, Any] = field(default_factory=dict)
    address: str = ""
    community_name: str = ""
    area_sqm: float | None = None


@dataclass(slots=True)
class RawAuctionDetail:
    """Detail & announcement data fetched once per item (or on ended refresh)."""

    platform: str
    item_id: str
    url: str
    title: str = ""
    status: str = ""
    current_price: float | None = None
    initial_price: float | None = None
    consult_price: float | None = None
    deposit_price: float | None = None
    bid_count: int | None = None
    apply_count: int | None = None
    watch_count: int | None = None
    start_time: str | None = None
    end_time: str | None = None
    court_name: str = ""
    address: str = ""
    area_sqm: float | None = None
    notice_text: str = ""
    survey_text: str = ""
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedPropertyInfo:
    """Derived semantic attributes extracted from title, detail, and notice text."""

    property_key: str
    normalized_address: str
    community_name: str
    district: str
    property_type: str  # residential / commercial / parking / industrial / land / other
    is_partial_share: bool
    ownership_share: str
    area_sqm: float | None
    risk_tags: list[str] = field(default_factory=list)
    risk_level: str = "low"  # low / medium / high


@dataclass(slots=True)
class SyncSummary:
    run_id: int
    provider: str
    region_name: str
    status: str  # success / auth_required / error
    pages_fetched: int = 0
    items_seen: int = 0
    properties_created: int = 0
    rounds_upserted: int = 0
    details_enriched: int = 0
    ended_refreshed: int = 0
    message: str = ""
