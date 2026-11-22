from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fph.normalizer import VALID_PROPERTY_TYPES

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]


VALID_AUCTION_STATUSES: tuple[str, ...] = (
    "upcoming",
    "ongoing",
    "sold",
    "failed",
    "withdrawn",
    "suspended",
    "ended",
    "unknown",
)


def normalize_property_types(values: Iterable[str] | str | None) -> list[str]:
    """Validate and normalize property type filter list.

    Empty list (or 'all' / '*') means no property-type filtering.
    """
    if values is None:
        return []
    if isinstance(values, str):
        raw_items = [part.strip() for part in values.split(",")]
    elif isinstance(values, (bytes, bytearray, dict)) or not isinstance(values, Iterable):
        raise ValueError("allowed_property_types 必须是字符串或列表")
    else:
        raw_items = [str(item).strip() for item in values]

    tokens = [item.lower() for item in raw_items if item]
    if not tokens or any(tok in {"all", "*"} for tok in tokens):
        return []

    deduped: list[str] = []
    for tok in tokens:
        if tok not in VALID_PROPERTY_TYPES:
            raise ValueError(
                f"不支持的物业类型: {tok}（可选: {', '.join(VALID_PROPERTY_TYPES)}）"
            )
        if tok not in deduped:
            deduped.append(tok)
    return deduped


def normalize_statuses(values: Iterable[str] | str | None) -> list[str]:
    """Validate and normalize auction status filter list.

    Empty list (or 'all' / '*') means no status filtering.
    """
    if values is None:
        return []
    if isinstance(values, str):
        raw_items = [part.strip() for part in values.split(",")]
    elif isinstance(values, (bytes, bytearray, dict)) or not isinstance(values, Iterable):
        raise ValueError("enrich_statuses 必须是字符串或列表")
    else:
        raw_items = [str(item).strip() for item in values]

    tokens = [item.lower() for item in raw_items if item]
    if not tokens or any(tok in {"all", "*"} for tok in tokens):
        return []

    deduped: list[str] = []
    for tok in tokens:
        if tok not in VALID_AUCTION_STATUSES:
            raise ValueError(
                f"不支持的拍卖状态: {tok}（可选: {', '.join(VALID_AUCTION_STATUSES)}）"
            )
        if tok not in deduped:
            deduped.append(tok)
    return deduped


def _parse_int_option(raw: dict[str, Any], key: str, default: int, *, min_value: int) -> int:
    val = raw.get(key, default)
    if isinstance(val, bool):
        raise ValueError(f"{key} 必须是整数，当前值: {val!r}")
    try:
        parsed = int(val)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是整数，当前值: {val!r}") from exc
    if parsed < min_value:
        raise ValueError(f"{key} 必须 >= {min_value}，当前值: {parsed}")
    return parsed


@dataclass(slots=True)
class StorageConfig:
    db_path: Path = Path("data/fph.db")
    export_dir: Path = Path("exports")


@dataclass(slots=True)
class ProviderConfig:
    name: str = "taobao_ego"
    ego_browser_cmd: str = "ego-browser"
    task_space: str = "fph-auction-collector"
    timeout_seconds: int = 90
    page_timeout_seconds: int = 25
    min_delay_seconds: float = 3.0
    max_delay_seconds: float = 7.0
    max_pages_per_query: int = 5
    backfill_max_pages: int = 50
    max_detail_enrich_per_run: int = 20
    max_ended_refresh_per_run: int = 20
    allowed_property_types: list[str] = field(default_factory=list)
    lookback_days: int = 0
    enrich_statuses: list[str] = field(default_factory=list)
    stop_on_known_ended_page: bool = True
    max_detail_attempts: int = 3
    max_refresh_attempts: int = 3
    retry_cooldown_hours: int = 12
    ended_refresh_window_days: int = 30
    auth_wait_timeout_seconds: int = 900


@dataclass(slots=True)
class ScheduleConfig:
    interval_minutes: int = 360
    jitter_minutes: int = 15
    auto_enrich_details: bool = True
    auto_refresh_ended: bool = True


@dataclass(slots=True)
class RegionConfig:
    name: str
    province: str
    city: str
    location_code: str = ""
    category: str = "50025969"
    status_scopes: list[str] = field(default_factory=lambda: ["active", "ended"])
    custom_url: str = ""
    provider: str = ""
    jd_province_id: str = ""
    jd_city_id: str = ""
    jd_county_id: str = ""
    jd_category_id: str = ""
    allowed_property_types: list[str] | None = None


@dataclass(slots=True)
class AppConfig:
    storage: StorageConfig = field(default_factory=StorageConfig)
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    regions: list[RegionConfig] = field(default_factory=list)
    config_path: Path | None = field(default=None, repr=False, compare=False)


def resolve_allowed_property_types(
    config: AppConfig,
    region: RegionConfig | None = None,
) -> tuple[str, ...]:
    """Return effective allowed property types for a region (or global config)."""
    if region is not None and region.allowed_property_types is not None:
        return tuple(normalize_property_types(region.allowed_property_types))
    return tuple(normalize_property_types(config.provider.allowed_property_types))


def default_regions() -> list[RegionConfig]:
    return [
        RegionConfig(
            name="福州市-住宅",
            province="福建",
            city="福州",
            location_code="350100",
            category="50025969",
            status_scopes=["active", "ended"],
        ),
        RegionConfig(
            name="龙岩市-住宅",
            province="福建",
            city="龙岩",
            location_code="350800",
            category="50025969",
            status_scopes=["active", "ended"],
        ),
    ]


def load_config(path: str | Path | None = None) -> AppConfig:
    if path is None:
        return AppConfig(regions=default_regions())

    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {cfg_path}。可先运行 `fph init` 生成默认 config.toml。"
        )

    raw: dict[str, Any] = tomllib.loads(cfg_path.read_text(encoding="utf-8"))

    storage_raw = raw.get("storage", {})
    storage = StorageConfig(
        db_path=Path(storage_raw.get("db_path", "data/fph.db")),
        export_dir=Path(storage_raw.get("export_dir", "exports")),
    )

    provider_raw = raw.get("provider", {})
    provider = ProviderConfig(
        name=str(provider_raw.get("name", "taobao_ego")),
        ego_browser_cmd=str(provider_raw.get("ego_browser_cmd", "ego-browser")),
        task_space=str(provider_raw.get("task_space", "fph-auction-collector")),
        timeout_seconds=int(provider_raw.get("timeout_seconds", 90)),
        page_timeout_seconds=int(provider_raw.get("page_timeout_seconds", 25)),
        min_delay_seconds=float(provider_raw.get("min_delay_seconds", 3.0)),
        max_delay_seconds=float(provider_raw.get("max_delay_seconds", 7.0)),
        max_pages_per_query=int(provider_raw.get("max_pages_per_query", 5)),
        backfill_max_pages=_parse_int_option(provider_raw, "backfill_max_pages", 50, min_value=1),
        max_detail_enrich_per_run=int(provider_raw.get("max_detail_enrich_per_run", 20)),
        max_ended_refresh_per_run=int(provider_raw.get("max_ended_refresh_per_run", 20)),
        allowed_property_types=normalize_property_types(provider_raw.get("allowed_property_types")),
        lookback_days=_parse_int_option(provider_raw, "lookback_days", 0, min_value=0),
        enrich_statuses=normalize_statuses(provider_raw.get("enrich_statuses")),
        stop_on_known_ended_page=bool(provider_raw.get("stop_on_known_ended_page", True)),
        max_detail_attempts=_parse_int_option(provider_raw, "max_detail_attempts", 3, min_value=1),
        max_refresh_attempts=_parse_int_option(provider_raw, "max_refresh_attempts", 3, min_value=1),
        retry_cooldown_hours=_parse_int_option(provider_raw, "retry_cooldown_hours", 12, min_value=0),
        ended_refresh_window_days=_parse_int_option(
            provider_raw, "ended_refresh_window_days", 30, min_value=0
        ),
        auth_wait_timeout_seconds=_parse_int_option(
            provider_raw, "auth_wait_timeout_seconds", 900, min_value=0
        ),
    )

    schedule_raw = raw.get("schedule", {})
    schedule = ScheduleConfig(
        interval_minutes=int(schedule_raw.get("interval_minutes", 360)),
        jitter_minutes=int(schedule_raw.get("jitter_minutes", 15)),
        auto_enrich_details=bool(schedule_raw.get("auto_enrich_details", True)),
        auto_refresh_ended=bool(schedule_raw.get("auto_refresh_ended", True)),
    )

    regions: list[RegionConfig] = []
    for item in raw.get("regions", []):
        region_allowed = (
            normalize_property_types(item["allowed_property_types"])
            if "allowed_property_types" in item
            else None
        )
        regions.append(
            RegionConfig(
                name=str(item["name"]),
                province=str(item.get("province", "")),
                city=str(item.get("city", "")),
                location_code=str(item.get("location_code", "")),
                category=str(item.get("category", "50025969")),
                status_scopes=[str(s) for s in item.get("status_scopes", ["active", "ended"])],
                custom_url=str(item.get("custom_url", "")),
                provider=str(item.get("provider", "")),
                jd_province_id=str(item.get("jd_province_id", "")),
                jd_city_id=str(item.get("jd_city_id", "")),
                jd_county_id=str(item.get("jd_county_id", "")),
                jd_category_id=str(item.get("jd_category_id", "")),
                allowed_property_types=region_allowed,
            )
        )

    if not regions:
        regions = default_regions()

    return AppConfig(
        storage=storage,
        provider=provider,
        schedule=schedule,
        regions=regions,
        config_path=cfg_path.resolve(),
    )
