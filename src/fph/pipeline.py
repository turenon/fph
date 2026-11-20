from __future__ import annotations

import logging
import random
import time
from typing import Callable

from fph.config import AppConfig, RegionConfig
from fph.db import AuctionDB
from fph.models import SyncSummary
from fph.providers.base import AuctionProvider, AuthRequiredError, ProviderError
from fph.providers.taobao_ego import TaobaoEgoProvider
from fph.providers.taobao_urls import expand_status_queries

logger = logging.getLogger(__name__)


def build_provider(config: AppConfig) -> AuctionProvider:
    name = config.provider.name.lower()
    if name == "taobao_ego":
        return TaobaoEgoProvider(config.provider)
    raise ValueError(f"不支持的 provider: {config.provider.name}（当前支持: taobao_ego）")


class SyncPipeline:
    """Layered, low-frequency auction sync pipeline:
    1) List sync (active + ended scopes)
    2) One-time detail & announcement enrichment for newly discovered items
    3) Result refresh for items whose end_time has passed
    """

    def __init__(
        self,
        config: AppConfig,
        db: AuctionDB,
        provider: AuctionProvider,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.db = db
        self.provider = provider
        self._sleep = sleep_fn

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
        run_id = self.db.start_sync_run(self.provider.platform, region.name)
        summary = SyncSummary(
            run_id=run_id,
            provider=self.provider.platform,
            region_name=region.name,
            status="success",
        )
        scopes = status_scopes or region.status_scopes
        page_limit = max_pages if max_pages is not None else self.config.provider.max_pages_per_query

        try:
            for scope in scopes:
                for label, st_param in expand_status_queries(scope):
                    for page in range(1, page_limit + 1):
                        logger.info("[%s] 采集列表 scope=%s(st=%s) page=%d", region.name, label, st_param, page)
                        res = self.provider.fetch_list_page(region, status_scope=st_param, page=page)
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
                        self._polite_delay()
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

    def enrich_missing_details(self, limit: int | None = None) -> tuple[int, str | None]:
        """Fetch detail & announcement text once for rounds without detail_fetched_at."""
        max_items = limit if limit is not None else self.config.provider.max_detail_enrich_per_run
        rows = self.db.list_rounds_needing_detail(self.provider.platform, limit=max_items)
        count = 0
        for idx, row in enumerate(rows):
            try:
                detail = self.provider.fetch_detail(row["item_id"], row["url"])
                if self.db.apply_detail(detail):
                    count += 1
            except AuthRequiredError as exc:
                return count, str(exc)
            except ProviderError as exc:
                logger.warning("补抓详情失败 item_id=%s: %s", row["item_id"], exc)
            if idx + 1 < len(rows):
                self._polite_delay()
        return count, None

    def refresh_ended_rounds(self, limit: int | None = None) -> tuple[int, str | None]:
        """Re-fetch final transaction price/status for auctions past their end_time."""
        max_items = limit if limit is not None else self.config.provider.max_ended_refresh_per_run
        rows = self.db.list_rounds_needing_ended_refresh(self.provider.platform, limit=max_items)
        count = 0
        for idx, row in enumerate(rows):
            try:
                detail = self.provider.fetch_detail(row["item_id"], row["url"])
                if self.db.apply_detail(detail):
                    count += 1
            except AuthRequiredError as exc:
                return count, str(exc)
            except ProviderError as exc:
                logger.warning("回刷结拍结果失败 item_id=%s: %s", row["item_id"], exc)
            if idx + 1 < len(rows):
                self._polite_delay()
        return count, None

    def run_full_cycle(
        self,
        *,
        region_filter: str | None = None,
        max_pages: int | None = None,
        enrich_details: bool = True,
        refresh_ended: bool = True,
    ) -> list[SyncSummary]:
        summaries: list[SyncSummary] = []
        auth_blocked = False

        for region in self.config.regions:
            if region_filter and region_filter not in region.name and region_filter not in region.city:
                continue
            summary = self.sync_region(region, max_pages=max_pages)
            summaries.append(summary)
            if summary.status == "auth_required":
                auth_blocked = True
                break

        if not auth_blocked and enrich_details:
            enriched, auth_err = self.enrich_missing_details()
            if summaries:
                summaries[-1].details_enriched = enriched
                if auth_err:
                    summaries[-1].status = "auth_required"
                    summaries[-1].message = auth_err
                self.db.finish_sync_run(summaries[-1])
            if auth_err:
                auth_blocked = True

        if not auth_blocked and refresh_ended:
            refreshed, auth_err = self.refresh_ended_rounds()
            if summaries:
                summaries[-1].ended_refreshed = refreshed
                if auth_err:
                    summaries[-1].status = "auth_required"
                    summaries[-1].message = auth_err
                self.db.finish_sync_run(summaries[-1])

        return summaries
