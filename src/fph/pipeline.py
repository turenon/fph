from __future__ import annotations

import logging
import platform
import random
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Callable, TypeVar

from fph.config import (
    AppConfig,
    RegionConfig,
    normalize_property_types,
    normalize_statuses,
    resolve_allowed_property_types,
)
from fph.db import AuctionDB
from fph.models import SyncSummary
from fph.normalizer import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    classify_property_type,
    normalize_datetime,
    normalize_status,
)
from fph.providers.base import AuctionProvider, AuthRequiredError, ProviderError
from fph.providers.jd_ego import JdEgoProvider
from fph.providers.taobao_ego import TaobaoEgoProvider

logger = logging.getLogger(__name__)
CN_TZ = timezone(timedelta(hours=8))
_T = TypeVar("_T")


def prompt_user_for_auth(exc: AuthRequiredError, *, timeout_seconds: int = 900) -> bool:
    """Prompt the user (macOS native dialog without polling focus, or TTY fallback) to complete captcha/login."""
    if timeout_seconds <= 0:
        return False

    space = exc.task_space or "fph-auction-collector"
    url_line = f"\n页面: {exc.url}" if exc.url else ""
    print(
        f"\n[需要人机验证] {exc}\n"
        f"请在 ego(lite) 右上角切入 Space `{space}` 完成验证/登录。{url_line}",
        flush=True,
    )

    if platform.system() == "Darwin" and shutil.which("osascript"):
        safe_msg = (
            f"{exc}\n\n请在 ego(lite) 右上角切入 Space「{space}」手动完成滑块或登录验证。\n"
            "完成后点击「已完成，继续采集」将原地重试当前页。"
        )
        if exc.url:
            safe_msg += f"\n\nURL: {exc.url}"
        escaped = safe_msg.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            "beep\n"
            f'display dialog "{escaped}" with title "FPH 采集需要人机验证" '
            'buttons {"保存断点退出", "已完成，继续采集"} '
            'default button "已完成，继续采集" '
            f"giving up after {int(timeout_seconds)}"
        )
        try:
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=int(timeout_seconds) + 15,
                check=False,
            )
            if proc.returncode == 0:
                if "gave up:true" in proc.stdout:
                    logger.warning("等待人工验证超时（%ds），将保存断点退出。", timeout_seconds)
                    return False
                return "已完成，继续采集" in proc.stdout
            if "-128" in (proc.stderr or ""):
                return False
        except (OSError, subprocess.SubprocessError) as dialog_exc:
            logger.debug("macOS 弹窗提示失败，尝试终端交互: %s", dialog_exc)

    if sys.stdin is not None and sys.stdin.isatty():
        try:
            ans = input("完成验证后按回车继续重试当前页，输入 q 并回车保存断点退出: ").strip().lower()
            return ans not in {"q", "quit", "exit", "n", "no"}
        except EOFError:
            return False

    return False


def _cutoff_datetime_str(days: int) -> str | None:
    if days > 0:
        return (datetime.now(CN_TZ) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return None


def _is_iso_date_prefix(value: str) -> bool:
    return (
        len(value) >= 10
        and value[:4].isdigit()
        and value[4] == "-"
        and value[5:7].isdigit()
        and value[7] == "-"
        and value[8:10].isdigit()
    )

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
        auth_handler: Callable[[AuthRequiredError], bool] | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self._sleep = sleep_fn
        self._auth_handler = auth_handler
        self._max_auth_retries_per_step = 5
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

    def _call_with_auth_retry(self, fn: Callable[[], _T]) -> _T:
        for attempt in range(self._max_auth_retries_per_step + 1):
            try:
                return fn()
            except AuthRequiredError as exc:
                if (
                    self._auth_handler is None
                    or attempt >= self._max_auth_retries_per_step
                    or not self._auth_handler(exc)
                ):
                    raise
                logger.info("人工验证已完成，原地重试当前请求...")
        raise RuntimeError("unreachable")

    def _resolve_provider_allowed_types(
        self,
        platform: str,
        *,
        region_filter: str | None = None,
        override: Sequence[str] | None = None,
    ) -> tuple[str, ...]:
        if override is not None:
            return tuple(normalize_property_types(override))
        target_norm = normalize_provider_name(platform)
        matched_regions = [
            r
            for r in self.config.regions
            if resolve_region_provider_name(self.config, r) == target_norm
            and (not region_filter or region_filter in r.name or region_filter in r.city)
        ]
        if not matched_regions:
            return resolve_allowed_property_types(self.config, None)
        merged: list[str] = []
        for r in matched_regions:
            allowed = resolve_allowed_property_types(self.config, r)
            if not allowed:
                return ()
            for t in allowed:
                if t not in merged:
                    merged.append(t)
        return tuple(merged)

    def sync_region(
        self,
        region: RegionConfig,
        *,
        max_pages: int | None = None,
        status_scopes: list[str] | None = None,
        allowed_property_types: Sequence[str] | None = None,
        active_only: bool = False,
        lookback_days: int | None = None,
        use_checkpoint: bool = True,
    ) -> SyncSummary:
        provider = self._get_provider_for_region(region)
        run_id = self.db.start_sync_run(provider.platform, region.name)
        summary = SyncSummary(
            run_id=run_id,
            provider=provider.platform,
            region_name=region.name,
            status="success",
        )
        scopes = list(status_scopes if status_scopes is not None else region.status_scopes)
        if active_only:
            filtered_scopes: list[str] = []
            for sc in scopes:
                sc_clean = (sc or "").strip().lower()
                if sc_clean == "ended":
                    continue
                mapped = "active" if sc_clean == "all" else sc_clean
                if mapped and mapped not in filtered_scopes:
                    filtered_scopes.append(mapped)
            scopes = filtered_scopes

        eff_lookback = self.config.provider.lookback_days if lookback_days is None else max(0, lookback_days)
        effective_use_checkpoint = use_checkpoint and (
            lookback_days is None or lookback_days == self.config.provider.lookback_days
        )
        cutoff_dt = _cutoff_datetime_str(eff_lookback)
        effective_allowed = (
            tuple(normalize_property_types(allowed_property_types))
            if allowed_property_types is not None
            else resolve_allowed_property_types(self.config, region)
        )
        has_fetched_any = False

        try:
            if not scopes:
                logger.info("[%s/%s] active_only 过滤后无活跃范围，跳过采集", provider.platform, region.name)
                return summary

            for scope in scopes:
                for label, st_param in provider.expand_status_queries(scope):
                    is_ended_query = (label or "").strip().lower() == "ended" or (scope or "").strip().lower() == "ended"
                    checkpoint_scope = label or scope
                    backfill_done = True
                    page = 1
                    query_page_limit = (
                        max_pages if max_pages is not None else self.config.provider.max_pages_per_query
                    )

                    if is_ended_query and effective_use_checkpoint:
                        cp = self.db.get_checkpoint(provider.platform, region.name, checkpoint_scope)
                        backfill_done = bool(cp["backfill_completed"]) if cp is not None else False
                        if not backfill_done:
                            page = int(cp["next_page"]) if (cp is not None and int(cp["next_page"]) >= 1) else 1
                            if max_pages is None:
                                query_page_limit = max(1, self.config.provider.backfill_max_pages)
                            logger.info(
                                "[%s/%s] 首次历史回填模式，从第 %d 页续采（本次最多 %d 页）",
                                provider.platform,
                                region.name,
                                page,
                                query_page_limit,
                            )
                    elif is_ended_query and not effective_use_checkpoint:
                        logger.info(
                            "[%s/%s] 短期时间窗更新模式（lookback=%d天），从第 1 页增量采集且不改写回填断点",
                            provider.platform,
                            region.name,
                            eff_lookback,
                        )

                    pages_in_query = 0
                    try:
                        while pages_in_query < query_page_limit:
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
                            res = self._call_with_auth_retry(
                                lambda p=page, st=st_param: provider.fetch_list_page(
                                    region, status_scope=st, page=p
                                )
                            )
                            summary.pages_fetched += 1
                            pages_in_query += 1

                            if not res.items:
                                logger.info("[%s] page=%d 无更多条目，停止翻页", region.name, page)
                                if is_ended_query and not backfill_done:
                                    self.db.save_checkpoint(
                                        provider.platform,
                                        region.name,
                                        checkpoint_scope,
                                        next_page=1,
                                        backfill_completed=True,
                                        last_status="completed",
                                    )
                                break

                            page_total = len(res.items)
                            page_old = 0
                            page_existing_terminal = 0
                            existing_statuses: dict[str, str] = (
                                self.db.get_round_statuses(provider.platform, [it.item_id for it in res.items])
                                if is_ended_query
                                else {}
                            )

                            for item in res.items:
                                summary.items_seen += 1
                                item_status = normalize_status(item.status, bid_count=item.bid_count)
                                item_dt = normalize_datetime(item.end_time) or normalize_datetime(item.start_time)

                                if (
                                    cutoff_dt
                                    and item_status not in ACTIVE_STATUSES
                                    and item_dt
                                    and _is_iso_date_prefix(item_dt)
                                    and item_dt < cutoff_dt
                                ):
                                    summary.items_skipped_old += 1
                                    page_old += 1
                                    continue

                                if (
                                    is_ended_query
                                    and existing_statuses.get(item.item_id) in TERMINAL_STATUSES
                                    and item_status in TERMINAL_STATUSES
                                ):
                                    summary.items_skipped_existing += 1
                                    page_existing_terminal += 1
                                    continue

                                if effective_allowed:
                                    ptype = classify_property_type(
                                        item.title,
                                        detail_text=item.address,
                                        category_hint=item.category_hint,
                                        purpose_hint=item.purpose_hint,
                                    )
                                    if ptype not in effective_allowed:
                                        summary.items_filtered += 1
                                        logger.debug(
                                            "[%s/%s] 过滤非目标物业类型 (%s): %s",
                                            provider.platform,
                                            region.name,
                                            ptype,
                                            item.title,
                                        )
                                        continue
                                _, _, created = self.db.upsert_list_item(item)
                                if created:
                                    summary.properties_created += 1
                                summary.rounds_upserted += 1

                            if is_ended_query and page_total > 0:
                                if cutoff_dt and page_old == page_total:
                                    logger.info(
                                        "[%s/%s] 已结束列表 page=%d 整页 (%d 条) 均早于截止时间 %s，提前停止翻页",
                                        provider.platform,
                                        region.name,
                                        page,
                                        page_total,
                                        cutoff_dt,
                                    )
                                    if not backfill_done:
                                        self.db.save_checkpoint(
                                            provider.platform,
                                            region.name,
                                            checkpoint_scope,
                                            next_page=1,
                                            backfill_completed=True,
                                            last_status="completed",
                                        )
                                    break
                                if (
                                    backfill_done
                                    and self.config.provider.stop_on_known_ended_page
                                    and (page_existing_terminal + page_old) == page_total
                                ):
                                    logger.info(
                                        "[%s/%s] 已结束列表 page=%d 整页 (%d 条) 均已入库终态或超期（已终态=%d, 超期=%d），提前停止翻页",
                                        provider.platform,
                                        region.name,
                                        page,
                                        page_total,
                                        page_existing_terminal,
                                        page_old,
                                    )
                                    break

                            if not res.has_next:
                                if is_ended_query and not backfill_done:
                                    self.db.save_checkpoint(
                                        provider.platform,
                                        region.name,
                                        checkpoint_scope,
                                        next_page=1,
                                        backfill_completed=True,
                                        last_status="completed",
                                    )
                                break

                            if pages_in_query >= query_page_limit:
                                if is_ended_query and not backfill_done:
                                    self.db.save_checkpoint(
                                        provider.platform,
                                        region.name,
                                        checkpoint_scope,
                                        next_page=page + 1,
                                        backfill_completed=False,
                                        last_status="partial",
                                    )
                                break

                            if is_ended_query and not backfill_done:
                                self.db.save_checkpoint(
                                    provider.platform,
                                    region.name,
                                    checkpoint_scope,
                                    next_page=page + 1,
                                    backfill_completed=False,
                                    last_status="running",
                                )
                            page += 1
                    except AuthRequiredError:
                        if is_ended_query and not backfill_done:
                            self.db.save_checkpoint(
                                provider.platform,
                                region.name,
                                checkpoint_scope,
                                next_page=page,
                                backfill_completed=False,
                                last_status="auth_required",
                            )
                        raise
                    except ProviderError:
                        if is_ended_query and not backfill_done:
                            self.db.save_checkpoint(
                                provider.platform,
                                region.name,
                                checkpoint_scope,
                                next_page=page,
                                backfill_completed=False,
                                last_status="error",
                            )
                        raise
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
        except Exception as exc:
            summary.status = "error"
            summary.message = str(exc)
            logger.exception("[%s] 采集发生未预期异常", region.name)
            raise
        finally:
            self.db.finish_sync_run(summary)

        return summary

    def enrich_missing_details(
        self,
        limit: int | None = None,
        *,
        provider_filter: str | None = None,
        region_filter: str | None = None,
        allowed_property_types: Sequence[str] | None = None,
        allowed_statuses: Sequence[str] | str | None = None,
        allowed_districts: Sequence[str] | None = None,
        lookback_days: int | None = None,
    ) -> tuple[int, str | None]:
        """Fetch detail & announcement text once for rounds without detail_fetched_at."""
        remaining = limit if limit is not None else self.config.provider.max_detail_enrich_per_run
        total_count = 0
        has_fetched_any = False

        eff_statuses = (
            tuple(normalize_statuses(allowed_statuses))
            if allowed_statuses is not None
            else tuple(normalize_statuses(self.config.provider.enrich_statuses))
        )
        eff_districts = (
            tuple(str(d).strip() for d in allowed_districts if str(d).strip())
            if allowed_districts is not None
            else tuple(self.config.provider.enrich_districts)
        )
        eff_lookback = self.config.provider.lookback_days if lookback_days is None else max(0, lookback_days)
        min_date = _cutoff_datetime_str(eff_lookback)

        for provider in self._iter_target_providers(provider_filter):
            if remaining <= 0:
                break
            eff_types = self._resolve_provider_allowed_types(
                provider.platform,
                region_filter=region_filter,
                override=allowed_property_types,
            )
            rows = self.db.list_rounds_needing_detail(
                provider.platform,
                limit=remaining,
                allowed_property_types=eff_types or None,
                allowed_statuses=eff_statuses or None,
                allowed_districts=eff_districts or None,
                min_date=min_date,
                max_attempts=self.config.provider.max_detail_attempts,
                cooldown_hours=self.config.provider.retry_cooldown_hours,
            )
            for row in rows:
                if has_fetched_any:
                    self._polite_delay()
                has_fetched_any = True
                try:
                    detail = self._call_with_auth_retry(
                        lambda iid=row["item_id"], u=row["url"]: provider.fetch_detail(iid, u)
                    )
                    if self.db.apply_detail(detail):
                        total_count += 1
                    remaining -= 1
                except AuthRequiredError as exc:
                    return total_count, str(exc)
                except ProviderError as exc:
                    self.db.record_detail_attempt(provider.platform, row["item_id"])
                    logger.warning("补抓详情失败 platform=%s item_id=%s: %s", provider.platform, row["item_id"], exc)
                    remaining -= 1
        return total_count, None

    def refresh_ended_rounds(
        self,
        limit: int | None = None,
        *,
        provider_filter: str | None = None,
        region_filter: str | None = None,
        allowed_property_types: Sequence[str] | None = None,
        lookback_days: int | None = None,
    ) -> tuple[int, str | None]:
        """Re-fetch final transaction price/status for auctions past their end_time."""
        remaining = limit if limit is not None else self.config.provider.max_ended_refresh_per_run
        total_count = 0
        has_fetched_any = False

        eff_lookback = self.config.provider.lookback_days if lookback_days is None else max(0, lookback_days)
        window_days = max(0, self.config.provider.ended_refresh_window_days)
        positive_days = [d for d in (window_days, eff_lookback) if d > 0]
        min_end_time = _cutoff_datetime_str(min(positive_days)) if positive_days else None

        for provider in self._iter_target_providers(provider_filter):
            if remaining <= 0:
                break
            eff_types = self._resolve_provider_allowed_types(
                provider.platform,
                region_filter=region_filter,
                override=allowed_property_types,
            )
            rows = self.db.list_rounds_needing_ended_refresh(
                provider.platform,
                limit=remaining,
                allowed_property_types=eff_types or None,
                min_end_time=min_end_time,
                max_attempts=self.config.provider.max_refresh_attempts,
                cooldown_hours=self.config.provider.retry_cooldown_hours,
            )
            for row in rows:
                if has_fetched_any:
                    self._polite_delay()
                has_fetched_any = True
                try:
                    detail = self._call_with_auth_retry(
                        lambda iid=row["item_id"], u=row["url"]: provider.fetch_detail(iid, u)
                    )
                    if self.db.apply_detail(detail):
                        total_count += 1
                        new_status = self.db.get_round_status(provider.platform, row["item_id"])
                        if new_status not in TERMINAL_STATUSES:
                            self.db.record_refresh_attempt(provider.platform, row["item_id"])
                            logger.info(
                                "结拍回刷后仍未进入终态 platform=%s item_id=%s status=%s，已记录退避尝试",
                                provider.platform,
                                row["item_id"],
                                new_status or "unknown",
                            )
                    remaining -= 1
                except AuthRequiredError as exc:
                    return total_count, str(exc)
                except ProviderError as exc:
                    self.db.record_refresh_attempt(provider.platform, row["item_id"])
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
        allowed_property_types: Sequence[str] | None = None,
        active_only: bool = False,
        lookback_days: int | None = None,
        enrich_statuses: Sequence[str] | str | None = None,
        use_checkpoint: bool = True,
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

            summary = self.sync_region(
                region,
                max_pages=max_pages,
                allowed_property_types=allowed_property_types,
                active_only=active_only,
                lookback_days=lookback_days,
                use_checkpoint=use_checkpoint,
            )
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
                    region_filter=region_filter,
                    allowed_property_types=allowed_property_types,
                    allowed_statuses=enrich_statuses,
                    lookback_days=lookback_days,
                )
                remaining_enrich = max(0, remaining_enrich - enriched)
                target_sum = last_summary_by_platform.get(drv.platform)
                if target_sum is not None:
                    target_sum.details_enriched += enriched
                    if auth_err and target_sum.status != "error":
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
                    region_filter=region_filter,
                    allowed_property_types=allowed_property_types,
                    lookback_days=lookback_days,
                )
                remaining_refresh = max(0, remaining_refresh - refreshed)
                target_sum = last_summary_by_platform.get(drv.platform)
                if target_sum is not None:
                    target_sum.ended_refreshed += refreshed
                    if auth_err and target_sum.status != "error":
                        target_sum.status = "auth_required"
                        target_sum.message = auth_err
                    self.db.finish_sync_run(target_sum)
                if auth_err:
                    blocked_providers.add(drv.platform)

        return summaries
