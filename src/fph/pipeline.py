from __future__ import annotations

import logging
import random
import time
from typing import Callable

from fph.config import AppConfig, RegionConfig
from fph.db import AuctionDB
from fph.models import SyncSummary
from fph.providers.base import AuctionProvider, AuthRequiredError, ProviderError
from fph.providers.jd_ego import JdEgoProvider
from fph.providers.taobao_ego import TaobaoEgoProvider

logger = logging.getLogger(__name__)

_PROVIDER_ALIASES: dict[str, str] = {
    "taobao": "taobao_ego",
    "taobao_ego": "taobao_ego",
    "jd": "jd_ego",
    "jd_ego": "jd_ego",
}


def normalize_provider_name(name: str) -> str:
    clean = (name or "").strip().lower()
    return _PROVIDER_ALIASES.get(clean, clean)


def resolve_region_provider_name(config: AppConfig, region: RegionConfig) -> str:
    if region.provider.strip():
        return normalize_provider_name(region.provider)
    cfg_name = normalize_provider_name(config.provider.name)
    if cfg_name not in {"", "multi", "auto"}:
        return cfg_name
    return "taobao_ego"


def build_provider(config: AppConfig, provider_name: str | None = None) -> AuctionProvider:
    raw_name = provider_name if provider_name is not None else config.provider.name
    name = normalize_provider_name(raw_name)
    if name in {"", "multi", "auto"}:
        name = "taobao_ego"
    if name == "taobao_ego":
        return TaobaoEgoProvider(config.provider)
    if name == "jd_ego":
        return JdEgoProvider(config.provider)
    raise ValueError(f"不支持的 provider: {raw_name}（当前支持: taobao_ego, jd_ego）")


def build_providers(config: AppConfig) -> dict[str, AuctionProvider]:
    """Instantiate deduplicated provider drivers referenced by config regions."""
    names: list[str] = []
    for region in config.regions:
        pname = resolve_region_provider_name(config, region)
        if pname not in names:
            names.append(pname)
    if not names:
        fallback = normalize_provider_name(config.provider.name)
        names.append(fallback if fallback not in {"", "multi", "auto"} else "taobao_ego")
    return {name: build_provider(config, name) for name in names}


class SyncPipeline:
    """Layered, low-frequency multi-platform auction sync pipeline:
    1) List sync (active + ended scopes)
    2) One-time detail & announcement enrichment for newly discovered items
    3) Result refresh for items whose end_time has passed
    """

    def __init__(
        self,
        config: AppConfig,
        db: AuctionDB,
        provider: AuctionProvider | None = None,
        *,
        providers: dict[str, AuctionProvider] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.db = db
        self._sleep = sleep_fn
        self._providers: dict[str, AuctionProvider] = {}
        self._single_provider_mode = False

        if providers is not None:
            for key, drv in providers.items():
                self._register_provider(key, drv)
            self.provider = next(iter(providers.values())) if providers else build_provider(config)
        elif provider is not None:
            self._single_provider_mode = True
            self.provider = provider
            self._register_provider(provider.platform, provider)
        else:
            built = build_providers(config)
            for key, drv in built.items():
                self._register_provider(key, drv)
            self.provider = next(iter(built.values()))

    def _register_provider(self, key: str, drv: AuctionProvider) -> None:
        self._providers[normalize_provider_name(key)] = drv
        self._providers[key.strip().lower()] = drv
        self._providers[drv.platform.strip().lower()] = drv
        self._providers[normalize_provider_name(drv.platform)] = drv

    def _get_provider_for_region(self, region: RegionConfig) -> AuctionProvider:
        pname = resolve_region_provider_name(self.config, region)
        if pname in self._providers:
            return self._providers[pname]
        if self._single_provider_mode:
            return self.provider
        drv = build_provider(self.config, pname)
        self._register_provider(pname, drv)
        return drv

    def _iter_target_providers(self, provider_filter: str | None = None) -> list[AuctionProvider]:
        target_norm = normalize_provider_name(provider_filter) if provider_filter else None
        unique: list[AuctionProvider] = []
        seen_ids: set[int] = set()
        for drv in self._providers.values():
            if id(drv) in seen_ids:
                continue
            seen_ids.add(id(drv))
            if target_norm and normalize_provider_name(drv.platform) != target_norm:
                continue
            unique.append(drv)
        return unique

    def _polite_delay(self) -> None:
        lo = max(0.0, self.config.provider.min_delay_seconds)
        hi = max(lo, self.config.provider.max_delay_seconds)
        if hi > 0:
            self._sleep(random.uniform(lo, hi))

    def sync_region(
        self,
        region: RegionConfig,
        *,
        max_pages: int | None = None,
        status_scopes: list[str] | None = None,
    ) -> SyncSummary:
        provider = self._get_provider_for_region(region)
        run_id = self.db.start_sync_run(provider.platform, region.name)
        summary = SyncSummary(
            run_id=run_id,
            provider=provider.platform,
            region_name=region.name,
            status="success",
        )
        scopes = status_scopes or region.status_scopes
        page_limit = max_pages if max_pages is not None else self.config.provider.max_pages_per_query
        has_fetched_any = False

        try:
            for scope in scopes:
                for label, st_param in provider.expand_status_queries(scope):
                    for page in range(1, page_limit + 1):
                        if has_fetched_any:
                            self._polite_delay()
                        has_fetched_any = True

                        logger.info(
                            "[%s/%s] 采集列表 scope=%s(st=%s) page=%d",
                            provider.platform,
                            region.name,
                            label,
                            st_param or "-",
                            page,
                        )
                        res = provider.fetch_list_page(region, status_scope=st_param, page=page)
                        summary.pages_fetched += 1

                        if not res.items:
                            logger.info("[%s] page=%d 无更多条目，停止翻页", region.name, page)
                            break

                        for item in res.items:
                            _, _, created = self.db.upsert_list_item(item)
                            summary.items_seen += 1
                            if created:
                                summary.properties_created += 1
                            summary.rounds_upserted += 1

                        if not res.has_next or page >= page_limit:
                            break
        except AuthRequiredError as exc:
            summary.status = "auth_required"
            summary.message = (
                f"{exc}（请打开本机 ego(lite) -> 右上角 Space 切入 `{exc.task_space or self.config.provider.task_space}` "
                f"完成登录或验证后重跑）"
            )
            logger.warning("[%s] %s", region.name, summary.message)
        except ProviderError as exc:
            summary.status = "error"
            summary.message = str(exc)
            logger.error("[%s] 采集失败: %s", region.name, exc)
        finally:
            self.db.finish_sync_run(summary)

        return summary

    def enrich_missing_details(
        self,
        limit: int | None = None,
        *,
        provider_filter: str | None = None,
    ) -> tuple[int, str | None]:
        """Fetch detail & announcement text once for rounds without detail_fetched_at."""
        remaining = limit if limit is not None else self.config.provider.max_detail_enrich_per_run
        total_count = 0
        has_fetched_any = False

        for provider in self._iter_target_providers(provider_filter):
            if remaining <= 0:
                break
            rows = self.db.list_rounds_needing_detail(provider.platform, limit=remaining)
            for row in rows:
                if has_fetched_any:
                    self._polite_delay()
                has_fetched_any = True
                try:
                    detail = provider.fetch_detail(row["item_id"], row["url"])
                    if self.db.apply_detail(detail):
                        total_count += 1
                    remaining -= 1
                except AuthRequiredError as exc:
                    return total_count, str(exc)
                except ProviderError as exc:
                    logger.warning("补抓详情失败 platform=%s item_id=%s: %s", provider.platform, row["item_id"], exc)
                    remaining -= 1
        return total_count, None

    def refresh_ended_rounds(
        self,
        limit: int | None = None,
        *,
        provider_filter: str | None = None,
    ) -> tuple[int, str | None]:
        """Re-fetch final transaction price/status for auctions past their end_time."""
        remaining = limit if limit is not None else self.config.provider.max_ended_refresh_per_run
        total_count = 0
        has_fetched_any = False

        for provider in self._iter_target_providers(provider_filter):
            if remaining <= 0:
                break
            rows = self.db.list_rounds_needing_ended_refresh(provider.platform, limit=remaining)
            for row in rows:
                if has_fetched_any:
                    self._polite_delay()
                has_fetched_any = True
                try:
                    detail = provider.fetch_detail(row["item_id"], row["url"])
                    if self.db.apply_detail(detail):
                        total_count += 1
                    remaining -= 1
                except AuthRequiredError as exc:
                    return total_count, str(exc)
                except ProviderError as exc:
                    logger.warning("回刷结拍结果失败 platform=%s item_id=%s: %s", provider.platform, row["item_id"], exc)
                    remaining -= 1
        return total_count, None

    def run_full_cycle(
        self,
        *,
        region_filter: str | None = None,
        provider_filter: str | None = None,
        max_pages: int | None = None,
        enrich_details: bool = True,
        refresh_ended: bool = True,
    ) -> list[SyncSummary]:
        summaries: list[SyncSummary] = []
        last_summary_by_platform: dict[str, SyncSummary] = {}
        blocked_providers: set[str] = set()
        target_provider_norm = normalize_provider_name(provider_filter) if provider_filter else None

        for region in self.config.regions:
            if region_filter and region_filter not in region.name and region_filter not in region.city:
                continue
            region_provider_norm = resolve_region_provider_name(self.config, region)
            if target_provider_norm and region_provider_norm != target_provider_norm:
                continue

            drv = self._get_provider_for_region(region)
            if drv.platform in blocked_providers:
                continue

            summary = self.sync_region(region, max_pages=max_pages)
            summaries.append(summary)
            last_summary_by_platform[summary.provider] = summary
            if summary.status == "auth_required":
                blocked_providers.add(summary.provider)

        if enrich_details:
            remaining_enrich = self.config.provider.max_detail_enrich_per_run
            for drv in self._iter_target_providers(provider_filter):
                if remaining_enrich <= 0:
                    break
                if drv.platform in blocked_providers:
                    continue
                enriched, auth_err = self.enrich_missing_details(
                    limit=remaining_enrich,
                    provider_filter=drv.platform,
                )
                remaining_enrich = max(0, remaining_enrich - enriched)
                target_sum = last_summary_by_platform.get(drv.platform) or (summaries[-1] if summaries else None)
                if target_sum is not None:
                    target_sum.details_enriched += enriched
                    if auth_err:
                        target_sum.status = "auth_required"
                        target_sum.message = auth_err
                    self.db.finish_sync_run(target_sum)
                if auth_err:
                    blocked_providers.add(drv.platform)

        if refresh_ended:
            remaining_refresh = self.config.provider.max_ended_refresh_per_run
            for drv in self._iter_target_providers(provider_filter):
                if remaining_refresh <= 0:
                    break
                if drv.platform in blocked_providers:
                    continue
                refreshed, auth_err = self.refresh_ended_rounds(
                    limit=remaining_refresh,
                    provider_filter=drv.platform,
                )
                remaining_refresh = max(0, remaining_refresh - refreshed)
                target_sum = last_summary_by_platform.get(drv.platform) or (summaries[-1] if summaries else None)
                if target_sum is not None:
                    target_sum.ended_refreshed += refreshed
                    if auth_err:
                        target_sum.status = "auth_required"
                        target_sum.message = auth_err
                    self.db.finish_sync_run(target_sum)
                if auth_err:
                    blocked_providers.add(drv.platform)

        return summaries
