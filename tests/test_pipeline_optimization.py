from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from fph.config import AppConfig, ProviderConfig, RegionConfig, StorageConfig
from fph.db import AuctionDB
from fph.models import RawAuctionDetail, RawAuctionItem
from fph.pipeline import SyncPipeline
from fph.providers.base import AuctionProvider, AuthRequiredError, PageResult, ProviderError

CN_TZ = timezone(timedelta(hours=8))


def _dt_days_ago(days: int) -> str:
    return (datetime.now(CN_TZ) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _dt_hours_ago(hours: int) -> str:
    return (datetime.now(CN_TZ) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _dt_days_ahead(days: int) -> str:
    return (datetime.now(CN_TZ) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


class _StubProvider(AuctionProvider):
    platform = "taobao"

    def __init__(
        self,
        *,
        list_pages: dict[tuple[str, int], PageResult] | None = None,
        detail_handler: Callable[[str, str], RawAuctionDetail] | None = None,
    ) -> None:
        self.list_pages = list_pages or {}
        self.detail_handler = detail_handler
        self.list_calls: list[tuple[str, int]] = []
        self.detail_calls: list[str] = []

    def check_ready(self) -> tuple[bool, str]:
        return True, "stub ready"

    def expand_status_queries(self, status_scope: str) -> list[tuple[str, str]]:
        mapping = {
            "active": [("ongoing", "0"), ("upcoming", "1")],
            "ongoing": [("ongoing", "0")],
            "upcoming": [("upcoming", "1")],
            "ended": [("ended", "2")],
            "all": [("all", "-1")],
        }
        return mapping.get(status_scope, [("all", "-1")])

    def fetch_list_page(self, region: RegionConfig, status_scope: str, page: int) -> PageResult:
        self.list_calls.append((status_scope, page))
        return self.list_pages.get((status_scope, page), PageResult(items=[], page=page, has_next=False))

    def fetch_detail(self, item_id: str, url: str) -> RawAuctionDetail:
        self.detail_calls.append(item_id)
        if self.detail_handler is not None:
            return self.detail_handler(item_id, url)
        return RawAuctionDetail(
            platform=self.platform,
            item_id=item_id,
            url=url,
            notice_text="标的物建筑面积100平方米，已腾空。",
        )


def _make_item(
    item_id: str,
    *,
    status: str = "sold",
    end_time: str | None = None,
    start_time: str | None = None,
    bid_count: int | None = None,
    title: str | None = None,
) -> RawAuctionItem:
    return RawAuctionItem(
        platform="taobao",
        item_id=item_id,
        url=f"https://sf-item.taobao.com/sf_item/{item_id}.htm",
        title=title or f"【二拍】福州市鼓楼区温泉花园{item_id}幢501室住宅",
        province="福建",
        city="福州",
        status=status,
        initial_price=1_500_000,
        current_price=1_600_000,
        consult_price=2_000_000,
        bid_count=bid_count,
        start_time=start_time,
        end_time=end_time,
        category_hint="50025969",
    )


class PipelineOptimizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "opt.db"
        self.db = AuctionDB(self.db_path)
        self.region = RegionConfig(
            name="福州市-住宅",
            province="福建",
            city="福州",
            location_code="350100",
            status_scopes=["active", "ended"],
        )

    def tearDown(self) -> None:
        self.db.close()
        self.tmpdir.cleanup()

    def _build_config(self, **provider_kwargs: object) -> AppConfig:
        base: dict[str, object] = {
            "min_delay_seconds": 0.0,
            "max_delay_seconds": 0.0,
            "max_pages_per_query": 5,
            "allowed_property_types": ["residential"],
        }
        base.update(provider_kwargs)
        return AppConfig(
            storage=StorageConfig(db_path=self.db_path),
            provider=ProviderConfig(**base),  # type: ignore[arg-type]
            regions=[self.region],
        )

    def test_lookback_window_skips_old_ended_and_allows_active(self) -> None:
        provider = _StubProvider(
            list_pages={
                ("2", 1): PageResult(
                    items=[
                        _make_item("old_sold", status="sold", end_time=_dt_days_ago(900), bid_count=3),
                        _make_item("recent_sold", status="sold", end_time=_dt_days_ago(30), bid_count=2),
                        _make_item("old_ongoing", status="ongoing", start_time=_dt_days_ago(900)),
                        _make_item("old_upcoming", status="upcoming", start_time=_dt_days_ago(800)),
                    ],
                    page=1,
                    has_next=False,
                )
            }
        )
        cfg = self._build_config(lookback_days=730)
        pipeline = SyncPipeline(cfg, self.db, provider, sleep_fn=lambda _: None)

        summary = pipeline.sync_region(self.region, status_scopes=["ended"])
        self.assertEqual(summary.items_seen, 4)
        self.assertEqual(summary.items_skipped_old, 1)
        self.assertEqual(summary.rounds_upserted, 3)

        saved_ids = {
            r[0] for r in self.db.conn.execute("SELECT item_id FROM auction_rounds").fetchall()
        }
        self.assertEqual(saved_ids, {"recent_sold", "old_ongoing", "old_upcoming"})

        # Overriding lookback_days=0 disables the time-window cutoff
        summary_unbounded = pipeline.sync_region(self.region, status_scopes=["ended"], lookback_days=0)
        self.assertEqual(summary_unbounded.items_skipped_old, 0)
        self.assertEqual(self.db.get_round_status("taobao", "old_sold"), "sold")

    def test_ended_full_old_page_stops_pagination_early(self) -> None:
        provider = _StubProvider(
            list_pages={
                ("2", 1): PageResult(
                    items=[
                        _make_item("old_1", status="sold", end_time=_dt_days_ago(900), bid_count=1),
                        _make_item("old_2", status="failed", end_time=_dt_days_ago(1000), bid_count=0),
                    ],
                    page=1,
                    has_next=True,
                ),
                ("2", 2): PageResult(
                    items=[
                        _make_item("page2_item", status="sold", end_time=_dt_days_ago(10), bid_count=1),
                    ],
                    page=2,
                    has_next=False,
                ),
            }
        )
        cfg = self._build_config(lookback_days=730, max_pages_per_query=3)
        pipeline = SyncPipeline(cfg, self.db, provider, sleep_fn=lambda _: None)

        summary = pipeline.sync_region(self.region, status_scopes=["ended"])
        self.assertEqual(summary.pages_fetched, 1)
        self.assertEqual(summary.items_seen, 2)
        self.assertEqual(summary.items_skipped_old, 2)
        self.assertEqual(summary.rounds_upserted, 0)
        self.assertEqual(provider.list_calls, [("2", 1)])

    def test_ended_full_known_terminal_page_stops_pagination_early(self) -> None:
        # Pre-seed terminal rounds in DB & mark initial backfill completed
        self.db.upsert_list_item(_make_item("known_1", status="sold", end_time=_dt_days_ago(10), bid_count=3))
        self.db.upsert_list_item(_make_item("known_2", status="failed", end_time=_dt_days_ago(15), bid_count=0))
        self.db.save_checkpoint("taobao", self.region.name, "ended", next_page=1, backfill_completed=True, last_status="completed")

        list_pages = {
            ("2", 1): PageResult(
                items=[
                    _make_item("known_1", status="ended", end_time=_dt_days_ago(10), bid_count=3),
                    _make_item("known_2", status="ended", end_time=_dt_days_ago(15), bid_count=0),
                ],
                page=1,
                has_next=True,
            ),
            ("2", 2): PageResult(
                items=[
                    _make_item("new_on_p2", status="sold", end_time=_dt_days_ago(20), bid_count=1),
                ],
                page=2,
                has_next=False,
            ),
        }

        # stop_on_known_ended_page=True stops after page 1
        provider_stop = _StubProvider(list_pages=list_pages)
        cfg_stop = self._build_config(lookback_days=730, stop_on_known_ended_page=True)
        pipeline_stop = SyncPipeline(cfg_stop, self.db, provider_stop, sleep_fn=lambda _: None)

        summary = pipeline_stop.sync_region(self.region, status_scopes=["ended"])
        self.assertEqual(summary.pages_fetched, 1)
        self.assertEqual(summary.items_skipped_existing, 2)
        self.assertEqual(summary.rounds_upserted, 0)
        self.assertEqual(provider_stop.list_calls, [("2", 1)])
        self.assertIsNone(self.db.get_round_status("taobao", "new_on_p2"))

        # Mixed known-terminal + old items totaling the entire page also stops early
        mixed_provider = _StubProvider(
            list_pages={
                ("2", 1): PageResult(
                    items=[
                        _make_item("known_1", status="sold", end_time=_dt_days_ago(10), bid_count=3),
                        _make_item("very_old", status="failed", end_time=_dt_days_ago(900), bid_count=0),
                    ],
                    page=1,
                    has_next=True,
                ),
                ("2", 2): PageResult(
                    items=[_make_item("never_reached", status="sold", end_time=_dt_days_ago(5), bid_count=1)],
                    page=2,
                    has_next=False,
                ),
            }
        )
        pipeline_mixed = SyncPipeline(cfg_stop, self.db, mixed_provider, sleep_fn=lambda _: None)
        mixed_sum = pipeline_mixed.sync_region(self.region, status_scopes=["ended"])
        self.assertEqual(mixed_sum.pages_fetched, 1)
        self.assertEqual(mixed_sum.items_skipped_existing, 1)
        self.assertEqual(mixed_sum.items_skipped_old, 1)
        self.assertEqual(mixed_provider.list_calls, [("2", 1)])

        # stop_on_known_ended_page=False still skips rewriting known items, but continues to page 2
        provider_continue = _StubProvider(list_pages=list_pages)
        cfg_continue = self._build_config(lookback_days=730, stop_on_known_ended_page=False)
        pipeline_continue = SyncPipeline(cfg_continue, self.db, provider_continue, sleep_fn=lambda _: None)

        summary_cont = pipeline_continue.sync_region(self.region, status_scopes=["ended"])
        self.assertEqual(summary_cont.pages_fetched, 2)
        self.assertEqual(summary_cont.items_skipped_existing, 2)
        self.assertEqual(summary_cont.rounds_upserted, 1)
        self.assertEqual(provider_continue.list_calls, [("2", 1), ("2", 2)])
        self.assertEqual(self.db.get_round_status("taobao", "new_on_p2"), "sold")

    def test_active_only_skips_ended_maps_all_and_handles_empty(self) -> None:
        provider = _StubProvider(
            list_pages={
                ("0", 1): PageResult(
                    items=[_make_item("act_1", status="ongoing", end_time=_dt_days_ahead(3))],
                    page=1,
                    has_next=False,
                ),
                ("1", 1): PageResult(
                    items=[_make_item("up_1", status="upcoming", start_time=_dt_days_ahead(5))],
                    page=1,
                    has_next=False,
                ),
                ("2", 1): PageResult(
                    items=[_make_item("end_1", status="sold", end_time=_dt_days_ago(2), bid_count=1)],
                    page=1,
                    has_next=False,
                ),
            }
        )
        cfg = self._build_config()
        pipeline = SyncPipeline(cfg, self.db, provider, sleep_fn=lambda _: None)

        # 1. ["active", "ended"] -> only ongoing ("0") and upcoming ("1"), skips ended ("2")
        s1 = pipeline.sync_region(self.region, active_only=True)
        self.assertEqual(s1.status, "success")
        self.assertEqual(provider.list_calls, [("0", 1), ("1", 1)])
        self.assertIsNone(self.db.get_round_status("taobao", "end_1"))

        # 2. ["all"] -> mapped to "active" ("0", "1")
        provider.list_calls.clear()
        all_region = RegionConfig(name="福州-all", province="福建", city="福州", status_scopes=["all"])
        s2 = pipeline.sync_region(all_region, active_only=True)
        self.assertEqual(s2.status, "success")
        self.assertEqual(provider.list_calls, [("0", 1), ("1", 1)])

        # 3. ["ended"] only -> empty after filtering -> zero requests, finished summary
        provider.list_calls.clear()
        ended_region = RegionConfig(name="福州-ended", province="福建", city="福州", status_scopes=["ended"])
        s3 = pipeline.sync_region(ended_region, active_only=True)
        self.assertEqual(s3.status, "success")
        self.assertEqual(s3.pages_fetched, 0)
        self.assertEqual(provider.list_calls, [])
        run_row = self.db.conn.execute("SELECT status, finished_at FROM sync_runs WHERE id = ?", (s3.run_id,)).fetchone()
        self.assertEqual(run_row["status"], "success")
        self.assertIsNotNone(run_row["finished_at"])

    def test_enrich_statuses_filters_terminal_and_allows_override(self) -> None:
        self.db.upsert_list_item(_make_item("e_ongoing", status="ongoing", end_time=_dt_days_ahead(2)))
        self.db.upsert_list_item(_make_item("e_upcoming", status="upcoming", start_time=_dt_days_ahead(5)))
        self.db.upsert_list_item(_make_item("e_sold", status="sold", end_time=_dt_days_ago(5), bid_count=2))
        self.db.upsert_list_item(_make_item("e_failed", status="failed", end_time=_dt_days_ago(8), bid_count=0))
        self.db.upsert_list_item(_make_item("e_old_sold", status="sold", end_time=_dt_days_ago(900), bid_count=1))

        provider = _StubProvider()
        cfg = self._build_config(lookback_days=730, enrich_statuses=["ongoing", "upcoming"])
        pipeline = SyncPipeline(cfg, self.db, provider, sleep_fn=lambda _: None)

        # Default config: only ongoing & upcoming are enriched
        count1, err1 = pipeline.enrich_missing_details(limit=10)
        self.assertIsNone(err1)
        self.assertEqual(count1, 2)
        self.assertEqual(set(provider.detail_calls), {"e_ongoing", "e_upcoming"})

        # Explicit override: only sold within lookback window
        provider.detail_calls.clear()
        count2, err2 = pipeline.enrich_missing_details(limit=10, allowed_statuses=["sold"])
        self.assertIsNone(err2)
        self.assertEqual(count2, 1)
        self.assertEqual(provider.detail_calls, ["e_sold"])

        # Explicit override: [] (all statuses) still respects lookback_days=730
        provider.detail_calls.clear()
        count3, err3 = pipeline.enrich_missing_details(limit=10, allowed_statuses=[])
        self.assertIsNone(err3)
        self.assertEqual(count3, 1)
        self.assertEqual(provider.detail_calls, ["e_failed"])

        # Lookback override = 0 reaches old sold item
        provider.detail_calls.clear()
        count4, err4 = pipeline.enrich_missing_details(limit=10, allowed_statuses=[], lookback_days=0)
        self.assertIsNone(err4)
        self.assertEqual(count4, 1)
        self.assertEqual(provider.detail_calls, ["e_old_sold"])

    def test_provider_error_triggers_backoff_and_stops_at_max_attempts(self) -> None:
        self.db.upsert_list_item(_make_item("flaky_1", status="ongoing", end_time=_dt_days_ahead(3)))

        def failing_detail(item_id: str, url: str) -> RawAuctionDetail:
            raise ProviderError("network timeout")

        provider = _StubProvider(detail_handler=failing_detail)
        cfg = self._build_config(
            enrich_statuses=["ongoing", "upcoming"],
            max_detail_attempts=2,
            retry_cooldown_hours=12,
        )
        pipeline = SyncPipeline(cfg, self.db, provider, sleep_fn=lambda _: None)

        # Attempt 1 fails -> recorded
        c1, err1 = pipeline.enrich_missing_details(limit=5)
        self.assertEqual((c1, err1), (0, None))
        self.assertEqual(provider.detail_calls, ["flaky_1"])
        row = self.db.conn.execute(
            "SELECT detail_attempts, detail_last_attempt_at FROM auction_rounds WHERE item_id = 'flaky_1'"
        ).fetchone()
        self.assertEqual(row["detail_attempts"], 1)
        self.assertIsNotNone(row["detail_last_attempt_at"])

        # Immediate retry within 12h cooldown -> skipped
        provider.detail_calls.clear()
        c2, err2 = pipeline.enrich_missing_details(limit=5)
        self.assertEqual((c2, err2), (0, None))
        self.assertEqual(provider.detail_calls, [])

        # Expire cooldown -> Attempt 2 fails -> reaches max_detail_attempts=2
        self.db.conn.execute(
            "UPDATE auction_rounds SET detail_last_attempt_at = ? WHERE item_id = 'flaky_1'",
            (_dt_hours_ago(13),),
        )
        self.db.conn.commit()
        c3, err3 = pipeline.enrich_missing_details(limit=5)
        self.assertEqual((c3, err3), (0, None))
        self.assertEqual(provider.detail_calls, ["flaky_1"])
        attempts = self.db.conn.execute(
            "SELECT detail_attempts FROM auction_rounds WHERE item_id = 'flaky_1'"
        ).fetchone()[0]
        self.assertEqual(attempts, 2)

        # Even after cooldown expires again, max_detail_attempts=2 prevents any more attempts
        provider.detail_calls.clear()
        self.db.conn.execute(
            "UPDATE auction_rounds SET detail_last_attempt_at = ? WHERE item_id = 'flaky_1'",
            (_dt_hours_ago(48),),
        )
        self.db.conn.commit()
        c4, err4 = pipeline.enrich_missing_details(limit=5)
        self.assertEqual((c4, err4), (0, None))
        self.assertEqual(provider.detail_calls, [])

    def test_auth_required_does_not_consume_attempts_and_aborts_immediately(self) -> None:
        self.db.upsert_list_item(_make_item("auth_d1", status="ongoing", end_time=_dt_days_ahead(2)))
        self.db.upsert_list_item(_make_item("auth_d2", status="ongoing", end_time=_dt_days_ahead(3)))
        self.db.upsert_list_item(_make_item("auth_r1", status="ongoing", end_time=_dt_days_ago(2)))
        self.db.upsert_list_item(_make_item("auth_r2", status="ongoing", end_time=_dt_days_ago(3)))

        def auth_fail(item_id: str, url: str) -> RawAuctionDetail:
            raise AuthRequiredError("检测到滑块验证", url=url, task_space="test-space")

        provider = _StubProvider(detail_handler=auth_fail)
        cfg = self._build_config(
            enrich_statuses=["ongoing", "upcoming"],
            max_detail_attempts=2,
            max_refresh_attempts=2,
            retry_cooldown_hours=12,
        )
        pipeline = SyncPipeline(cfg, self.db, provider, sleep_fn=lambda _: None)

        # Enrich aborts on first AuthRequiredError without incrementing detail_attempts
        c_enr, err_enr = pipeline.enrich_missing_details(limit=5)
        self.assertEqual(c_enr, 0)
        self.assertIn("滑块验证", err_enr or "")
        self.assertEqual(len(provider.detail_calls), 1)
        for item_id in ("auth_d1", "auth_d2", "auth_r1", "auth_r2"):
            row = self.db.conn.execute(
                "SELECT detail_attempts, detail_last_attempt_at FROM auction_rounds WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            self.assertEqual(row["detail_attempts"], 0)
            self.assertIsNone(row["detail_last_attempt_at"])

        # Refresh aborts on first AuthRequiredError without incrementing refresh_attempts
        provider.detail_calls.clear()
        c_ref, err_ref = pipeline.refresh_ended_rounds(limit=5)
        self.assertEqual(c_ref, 0)
        self.assertIn("滑块验证", err_ref or "")
        self.assertEqual(len(provider.detail_calls), 1)
        for item_id in ("auth_r1", "auth_r2"):
            row = self.db.conn.execute(
                "SELECT refresh_attempts, refresh_last_attempt_at FROM auction_rounds WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            self.assertEqual(row["refresh_attempts"], 0)
            self.assertIsNone(row["refresh_last_attempt_at"])

    def test_refresh_ended_non_terminal_backoff_and_window_filtering(self) -> None:
        self.db.upsert_list_item(_make_item("stuck_1", status="ongoing", end_time=_dt_days_ago(2)))
        self.db.upsert_list_item(_make_item("resolve_1", status="ongoing", end_time=_dt_days_ago(3)))
        self.db.upsert_list_item(_make_item("too_old_refresh", status="ongoing", end_time=_dt_days_ago(45)))
        self.db.upsert_list_item(_make_item("mid_window", status="ongoing", end_time=_dt_days_ago(15)))

        def detail_handler(item_id: str, url: str) -> RawAuctionDetail:
            if item_id == "resolve_1":
                return RawAuctionDetail(
                    platform="taobao",
                    item_id=item_id,
                    url=url,
                    status="拍卖已成交",
                    current_price=1_700_000,
                    bid_count=5,
                )
            if item_id == "stuck_1":
                # Cannot parse terminal status -> remains 'ongoing'
                return RawAuctionDetail(
                    platform="taobao",
                    item_id=item_id,
                    url=url,
                    status="",
                    notice_text="公告正文正常但未展示结拍状态",
                )
            if item_id == "mid_window":
                raise ProviderError("temporary parse failure")
            return RawAuctionDetail(platform="taobao", item_id=item_id, url=url, status="流拍")

        provider = _StubProvider(detail_handler=detail_handler)
        cfg = self._build_config(
            lookback_days=730,
            ended_refresh_window_days=30,
            max_refresh_attempts=2,
            retry_cooldown_hours=12,
        )
        pipeline = SyncPipeline(cfg, self.db, provider, sleep_fn=lambda _: None)

        # Pass lookback_days=10 (stricter than ended_refresh_window_days=30):
        # skips both too_old_refresh (45d) and mid_window (15d)
        c1, err1 = pipeline.refresh_ended_rounds(limit=10, lookback_days=10)
        self.assertIsNone(err1)
        self.assertEqual(c1, 2)
        self.assertEqual(set(provider.detail_calls), {"stuck_1", "resolve_1"})
        self.assertEqual(self.db.get_round_status("taobao", "resolve_1"), "sold")
        self.assertEqual(self.db.get_round_status("taobao", "stuck_1"), "ongoing")

        # stuck_1 did not become terminal -> refresh_attempts incremented to 1
        stuck_row = self.db.conn.execute(
            "SELECT refresh_attempts, refresh_last_attempt_at FROM auction_rounds WHERE item_id = 'stuck_1'"
        ).fetchone()
        self.assertEqual(stuck_row["refresh_attempts"], 1)
        self.assertIsNotNone(stuck_row["refresh_last_attempt_at"])

        # resolve_1 became terminal -> refresh_attempts stayed 0
        res_row = self.db.conn.execute(
            "SELECT refresh_attempts FROM auction_rounds WHERE item_id = 'resolve_1'"
        ).fetchone()
        self.assertEqual(res_row["refresh_attempts"], 0)

        # Immediate re-run with default lookback (730d, effective window=30d):
        # stuck_1 is in cooldown, resolve_1 is terminal, too_old_refresh > 30d -> only mid_window tried and fails
        provider.detail_calls.clear()
        c2, err2 = pipeline.refresh_ended_rounds(limit=10)
        self.assertIsNone(err2)
        self.assertEqual(c2, 0)
        self.assertEqual(provider.detail_calls, ["mid_window"])
        mid_attempts = self.db.conn.execute(
            "SELECT refresh_attempts FROM auction_rounds WHERE item_id = 'mid_window'"
        ).fetchone()[0]
        self.assertEqual(mid_attempts, 1)

        # Expire cooldown for stuck_1 -> 2nd attempt hits max_refresh_attempts=2
        provider.detail_calls.clear()
        self.db.conn.execute(
            "UPDATE auction_rounds SET refresh_last_attempt_at = ? WHERE item_id = 'stuck_1'",
            (_dt_hours_ago(13),),
        )
        self.db.conn.commit()
        c3, err3 = pipeline.refresh_ended_rounds(limit=10)
        self.assertIsNone(err3)
        self.assertEqual(c3, 1)
        self.assertEqual(provider.detail_calls, ["stuck_1"])
        stuck_attempts = self.db.conn.execute(
            "SELECT refresh_attempts FROM auction_rounds WHERE item_id = 'stuck_1'"
        ).fetchone()[0]
        self.assertEqual(stuck_attempts, 2)

        # Expire cooldown again -> stuck_1 is permanently backed off (no infinite loop!)
        provider.detail_calls.clear()
        self.db.conn.execute(
            "UPDATE auction_rounds SET refresh_last_attempt_at = ? WHERE item_id = 'stuck_1'",
            (_dt_hours_ago(48),),
        )
        self.db.conn.commit()
        c4, err4 = pipeline.refresh_ended_rounds(limit=10)
        self.assertIsNone(err4)
        self.assertEqual(c4, 0)
        self.assertEqual(provider.detail_calls, [])

    def test_backfill_checkpoint_resume_and_switch_to_incremental(self) -> None:
        # Page 1: recent item; Page 2: raises AuthRequiredError first, then succeeds on resume;
        # Page 3: all old (>730d) -> completes backfill
        class _CheckpointProvider(_StubProvider):
            def __init__(self) -> None:
                super().__init__()
                self.fail_p2 = True

            def fetch_list_page(self, region: RegionConfig, status_scope: str, page: int) -> PageResult:
                self.list_calls.append((status_scope, page))
                if page == 1:
                    return PageResult(
                        items=[_make_item("cp_p1", status="sold", end_time=_dt_days_ago(5), bid_count=2)],
                        page=1,
                        has_next=True,
                    )
                if page == 2:
                    if self.fail_p2:
                        raise AuthRequiredError("滑块验证", url="https://sf.taobao.com", task_space="sp")
                    return PageResult(
                        items=[_make_item("cp_p2", status="sold", end_time=_dt_days_ago(20), bid_count=1)],
                        page=2,
                        has_next=True,
                    )
                if page == 3:
                    return PageResult(
                        items=[_make_item("cp_p3_old", status="sold", end_time=_dt_days_ago(900), bid_count=1)],
                        page=3,
                        has_next=True,
                    )
                return PageResult(items=[], page=page, has_next=False)

        prov = _CheckpointProvider()
        cfg = self._build_config(lookback_days=730, max_pages_per_query=1, backfill_max_pages=5)

        # Run 1: auth_handler returns False -> saves checkpoint at page 2, backfill_completed=False
        p1 = SyncPipeline(cfg, self.db, prov, sleep_fn=lambda _: None, auth_handler=lambda _: False)
        s1 = p1.sync_region(self.region, status_scopes=["ended"])
        self.assertEqual(s1.status, "auth_required")
        self.assertEqual(prov.list_calls, [("2", 1), ("2", 2)])
        cp1 = self.db.get_checkpoint("taobao", self.region.name, "ended")
        self.assertIsNotNone(cp1)
        assert cp1 is not None
        self.assertEqual(cp1["next_page"], 2)
        self.assertFalse(cp1["backfill_completed"])

        # Run 2: resumes directly from page 2 (uses backfill_max_pages=5, not max_pages_per_query=1!),
        # hits 2-year cutoff on page 3 -> marks backfill_completed=True
        prov.list_calls.clear()
        prov.fail_p2 = False
        s2 = p1.sync_region(self.region, status_scopes=["ended"])
        self.assertEqual(s2.status, "success")
        self.assertEqual(prov.list_calls, [("2", 2), ("2", 3)])
        cp2 = self.db.get_checkpoint("taobao", self.region.name, "ended")
        assert cp2 is not None
        self.assertTrue(cp2["backfill_completed"])
        self.assertEqual(cp2["next_page"], 1)
        self.assertEqual(self.db.get_round_status("taobao", "cp_p2"), "sold")

        # Run 3: incremental mode starts at page 1 and stops early on known-ended page
        prov.list_calls.clear()
        s3 = p1.sync_region(self.region, status_scopes=["ended"])
        self.assertEqual(s3.status, "success")
        self.assertEqual(prov.list_calls, [("2", 1)])
        self.assertEqual(s3.items_skipped_existing, 1)

    def test_auth_handler_retries_same_page_and_detail_in_place(self) -> None:
        class _AuthRetryProvider(_StubProvider):
            def __init__(self) -> None:
                super().__init__()
                self.list_failures = 1
                self.detail_failures = 1

            def fetch_list_page(self, region: RegionConfig, status_scope: str, page: int) -> PageResult:
                self.list_calls.append((status_scope, page))
                if status_scope == "0" and page == 1 and self.list_failures > 0:
                    self.list_failures -= 1
                    raise AuthRequiredError("需要滑块", url="https://sf.taobao.com", task_space="sp")
                if status_scope == "0" and page == 1:
                    return PageResult(
                        items=[_make_item("retry_ok", status="ongoing", end_time=_dt_days_ahead(3))],
                        page=1,
                        has_next=False,
                    )
                return PageResult(items=[], page=page, has_next=False)

            def fetch_detail(self, item_id: str, url: str) -> RawAuctionDetail:
                self.detail_calls.append(item_id)
                if self.detail_failures > 0:
                    self.detail_failures -= 1
                    raise AuthRequiredError("详情滑块", url=url, task_space="sp")
                return RawAuctionDetail(
                    platform="taobao",
                    item_id=item_id,
                    url=url,
                    notice_text="建筑面积100平方米，已腾空。",
                )

        prov = _AuthRetryProvider()
        prompts: list[str] = []

        def fake_auth_handler(exc: AuthRequiredError) -> bool:
            prompts.append(str(exc))
            return True

        cfg = self._build_config(enrich_statuses=["ongoing", "upcoming"])
        pipeline = SyncPipeline(cfg, self.db, prov, sleep_fn=lambda _: None, auth_handler=fake_auth_handler)
        sums = pipeline.run_full_cycle(active_only=True, enrich_details=True, refresh_ended=False)
        self.assertEqual(len(sums), 1)
        self.assertEqual(sums[0].status, "success")
        self.assertEqual(sums[0].rounds_upserted, 1)
        self.assertEqual(sums[0].details_enriched, 1)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(prov.list_calls, [("0", 1), ("0", 1), ("1", 1)])
        self.assertEqual(prov.detail_calls, ["retry_ok", "retry_ok"])

    def test_short_lookback_override_starts_at_page_1_without_overwriting_backfill_checkpoint(self) -> None:
        # Simulate an active 2-year backfill paused at next_page=15
        self.db.save_checkpoint(
            "taobao",
            self.region.name,
            "ended",
            next_page=15,
            backfill_completed=False,
            last_status="auth_required",
        )
        provider = _StubProvider(
            list_pages={
                ("2", 1): PageResult(
                    items=[
                        _make_item("recent_7d", status="sold", end_time=_dt_days_ago(2), bid_count=1),
                        _make_item("older_20d", status="sold", end_time=_dt_days_ago(20), bid_count=2),
                    ],
                    page=1,
                    has_next=True,
                ),
                ("2", 2): PageResult(
                    items=[
                        _make_item("older_40d", status="sold", end_time=_dt_days_ago(40), bid_count=1),
                    ],
                    page=2,
                    has_next=False,
                ),
            }
        )
        cfg = self._build_config(lookback_days=730, max_pages_per_query=1)
        pipeline = SyncPipeline(cfg, self.db, provider, sleep_fn=lambda _: None)

        summary = pipeline.sync_region(self.region, status_scopes=["ended"], lookback_days=7)
        self.assertEqual(summary.status, "success")
        self.assertEqual(provider.list_calls, [("2", 1)])
        self.assertEqual(summary.items_skipped_old, 1)
        self.assertEqual(summary.rounds_upserted, 1)
        self.assertEqual(self.db.get_round_status("taobao", "recent_7d"), "sold")

        # Existing 2-year backfill checkpoint at page 15 must remain untouched!
        cp = self.db.get_checkpoint("taobao", self.region.name, "ended")
        assert cp is not None
        self.assertEqual(cp["next_page"], 15)
        self.assertFalse(cp["backfill_completed"])


if __name__ == "__main__":
    unittest.main()
