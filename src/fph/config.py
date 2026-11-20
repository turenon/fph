from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]


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
    max_detail_enrich_per_run: int = 20
    max_ended_refresh_per_run: int = 20


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


@dataclass(slots=True)
class AppConfig:
    storage: StorageConfig = field(default_factory=StorageConfig)
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    regions: list[RegionConfig] = field(default_factory=list)


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
        max_detail_enrich_per_run=int(provider_raw.get("max_detail_enrich_per_run", 20)),
        max_ended_refresh_per_run=int(provider_raw.get("max_ended_refresh_per_run", 20)),
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
        regions.append(
            RegionConfig(
                name=str(item["name"]),
                province=str(item.get("province", "")),
                city=str(item.get("city", "")),
                location_code=str(item.get("location_code", "")),
                category=str(item.get("category", "50025969")),
                status_scopes=[str(s) for s in item.get("status_scopes", ["active", "ended"])],
                custom_url=str(item.get("custom_url", "")),
            )
        )

    if not regions:
        regions = default_regions()

    return AppConfig(
        storage=storage,
        provider=provider,
        schedule=schedule,
        regions=regions,
    )
