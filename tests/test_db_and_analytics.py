from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from fph.analytics import export_dataset, find_bargain_candidates, get_market_summary
from fph.config import (
    VALID_AUCTION_STATUSES,
    ProviderConfig,
    load_config,
    normalize_statuses,
)
from fph.db import CN_TZ, AuctionDB
from fph.models import RawAuctionDetail, RawAuctionItem, SyncSummary


class DbAndAnalyticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.db = AuctionDB(self.db_path)

    def tearDown(self) -> None:
        self.db.close()
        self.tmpdir.cleanup()

    def test_lifecycle_tracking_market_stats_and_bargains(self) -> None:
        # Property 1 (Fuzhou Gulou): Round 1 failed -> Round 2 sold
        self.db.upsert_list_item(
            RawAuctionItem(
                platform="taobao",
                item_id="fz_101_r1",
                url="https://sf-item.taobao.com/sf_item/fz_101_r1.htm",
                title="【一拍】福州市鼓楼区五四路温泉花园3幢501室房产",
                province="福建",
                city="福州",
                stage="一拍",
                status="failed",
                initial_price=2_000_000,
                current_price=2_000_000,
                consult_price=2_500_000,
                bid_count=0,
                end_time="2026-10-15 10:00:00",
                category_hint="50025969",
            )
        )
        self.db.upsert_list_item(
            RawAuctionItem(
                platform="taobao",
                item_id="fz_101_r2",
                url="https://sf-item.taobao.com/sf_item/fz_101_r2.htm",
                title="【二拍】福州市鼓楼区五四路温泉花园3#楼501室不动产（按现状拍卖）",
                province="福建",
                city="福州",
                stage="二拍",
                status="sold",
                initial_price=1_600_000,
                current_price=1_750_000,
                consult_price=2_500_000,
                bid_count=5,
                end_time="2026-11-10 10:00:00",
                category_hint="50025969",
            )
        )
        self.db.apply_detail(
            RawAuctionDetail(
                platform="taobao",
                item_id="fz_101_r2",
                url="https://sf-item.taobao.com/sf_item/fz_101_r2.htm",
                notice_text="标的物建筑面积100平方米，已腾空，按国家规定各自承担税费。",
            )
        )

        # Property 2 (Fuzhou Cangshan): Active bargain candidate (2nd round, 64% discount, low risk)
        self.db.upsert_list_item(
            RawAuctionItem(
                platform="taobao",
                item_id="fz_202_r2",
                url="https://sf-item.taobao.com/sf_item/fz_202_r2.htm",
                title="【二拍】福州市仓山区金山大道橘园洲花园8幢902室",
                province="福建",
                city="福州",
                stage="二拍",
                status="upcoming",
                initial_price=1_280_000,
                current_price=1_280_000,
                consult_price=2_000_000,
                apply_count=0,
                bid_count=0,
                end_time="2026-11-28 10:00:00",
                category_hint="50025969",
            )
        )
        self.db.apply_detail(
            RawAuctionDetail(
                platform="taobao",
                item_id="fz_202_r2",
                url="https://sf-item.taobao.com/sf_item/fz_202_r2.htm",
                notice_text="房屋建筑面积98.5平方米，空置状态，法院负责腾退。",
            )
        )

        # Property 3 (Longyan Xinluo): Parking spot (must be excluded from residential stats & bargains)
        self.db.upsert_list_item(
            RawAuctionItem(
                platform="taobao",
                item_id="ly_parking_1",
                url="https://sf-item.taobao.com/sf_item/ly_parking_1.htm",
                title="龙岩市新罗区某小区地下二层B08车位",
                province="福建",
                city="龙岩",
                stage="二拍",
                status="upcoming",
                initial_price=50_000,
                current_price=50_000,
                consult_price=100_000,
                category_hint="50025969",
            )
        )

        # Property 4 (Longyan Xinluo): Active residential but high risk (occupied + no eviction)
        self.db.upsert_list_item(
            RawAuctionItem(
                platform="taobao",
                item_id="ly_303_r2",
                url="https://sf-item.taobao.com/sf_item/ly_303_r2.htm",
                title="【二拍】龙岩市新罗区人民路中央苏区花园2幢301室",
                province="福建",
                city="龙岩",
                stage="二拍",
                status="ongoing",
                initial_price=600_000,
                current_price=600_000,
                consult_price=1_000_000,
                apply_count=1,
                category_hint="50025969",
            )
        )
        self.db.apply_detail(
            RawAuctionDetail(
                platform="taobao",
                item_id="ly_303_r2",
                url="https://sf-item.taobao.com/sf_item/ly_303_r2.htm",
                notice_text="建筑面积120平方米，目前有人占用，法院不负责腾退。",
            )
        )

        # Verify market stats for Fuzhou
        fz_stats = get_market_summary(self.db, city="福州")
        self.assertEqual(len(fz_stats), 1)
        row = fz_stats[0]
        self.assertEqual(row["unique_properties"], 2)
        self.assertEqual(row["total_rounds"], 3)
        self.assertEqual(row["sold_count"], 1)
        self.assertEqual(row["failed_count"], 1)
        self.assertEqual(row["clearance_rate_pct"], 50.0)
        self.assertEqual(row["avg_sold_discount"], 0.7)
        self.assertEqual(row["avg_sold_unit_price"], 17500.0)

        # Verify bargains exclude parking and high-risk items by default (max_risk_level='medium')
        bargains = find_bargain_candidates(self.db, max_discount=0.75, max_risk_level="medium")
        self.assertEqual(len(bargains), 1)
        self.assertEqual(bargains[0]["district"], "仓山区")
        self.assertEqual(bargains[0]["area_sqm"], 98.5)

        # Including high risk returns Longyan item too, still excluding parking
        bargains_all = find_bargain_candidates(self.db, max_discount=0.75, max_risk_level="high")
        self.assertEqual(len(bargains_all), 2)

        # Verify CSV (UTF-8-BOM) and JSON exports
        csv_path = Path(self.tmpdir.name) / "deals.csv"
        json_path = Path(self.tmpdir.name) / "all.json"
        self.assertEqual(export_dataset(self.db, csv_path, dataset="deals", fmt="csv"), 2)
        self.assertTrue(csv_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual(export_dataset(self.db, json_path, dataset="all", fmt="json"), 5)
        loaded = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(len(loaded), 5)

    def test_schema_migration_adds_retry_columns_to_legacy_db(self) -> None:
        legacy_path = Path(self.tmpdir.name) / "legacy.db"
        conn = sqlite3.connect(str(legacy_path))
        conn.executescript(
            """
            CREATE TABLE properties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                property_key TEXT NOT NULL UNIQUE,
                province TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                district TEXT NOT NULL DEFAULT '',
                community_name TEXT NOT NULL DEFAULT '',
                normalized_address TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                property_type TEXT NOT NULL DEFAULT 'other',
                is_partial_share INTEGER NOT NULL DEFAULT 0,
                ownership_share TEXT NOT NULL DEFAULT '100%',
                area_sqm REAL,
                risk_tags_json TEXT NOT NULL DEFAULT '[]',
                risk_level TEXT NOT NULL DEFAULT 'low',
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE auction_rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
                platform TEXT NOT NULL,
                item_id TEXT NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT '未知',
                status TEXT NOT NULL DEFAULT 'unknown',
                initial_price REAL,
                current_price REAL,
                consult_price REAL,
                deposit_price REAL,
                discount_rate REAL,
                unit_price REAL,
                bid_count INTEGER,
                apply_count INTEGER,
                watch_count INTEGER,
                start_time TEXT,
                end_time TEXT,
                court_name TEXT NOT NULL DEFAULT '',
                notice_text TEXT NOT NULL DEFAULT '',
                survey_text TEXT NOT NULL DEFAULT '',
                detail_fetched_at TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE(platform, item_id)
            );
            INSERT INTO properties (property_key, province, city, property_type, first_seen_at, updated_at)
            VALUES ('legacy_key', '福建', '福州', 'residential', '2026-01-01 00:00:00', '2026-01-01 00:00:00');
            INSERT INTO auction_rounds (
                property_id, platform, item_id, url, title, status, first_seen_at, last_seen_at
            ) VALUES (
                1, 'taobao', 'legacy_1', 'https://sf-item.taobao.com/sf_item/1.htm',
                '福州市鼓楼区温泉花园1幢101室', 'ongoing', '2026-01-01 00:00:00', '2026-01-01 00:00:00'
            );
            """
        )
        conn.commit()
        conn.close()

        with AuctionDB(legacy_path, read_only=True) as ro_db:
            ro_preview = ro_db.cleanup_stale_data(
                min_date="2024-01-01 00:00:00",
                allowed_property_types=["residential"],
                dry_run=True,
            )
            self.assertEqual(ro_preview["total_delete_rounds"], 0)
            ro_cols = {
                r["name"]
                for r in ro_db.conn.execute("PRAGMA table_info(auction_rounds)").fetchall()
            }
            self.assertNotIn("detail_attempts", ro_cols)

        with AuctionDB(legacy_path) as migrated:
            cols = {
                r["name"]
                for r in migrated.conn.execute("PRAGMA table_info(auction_rounds)").fetchall()
            }
            self.assertTrue(
                {
                    "detail_attempts",
                    "detail_last_attempt_at",
                    "refresh_attempts",
                    "refresh_last_attempt_at",
                }.issubset(cols)
            )
            row = migrated.conn.execute(
                "SELECT detail_attempts, detail_last_attempt_at, refresh_attempts, refresh_last_attempt_at "
                "FROM auction_rounds WHERE item_id = 'legacy_1'"
            ).fetchone()
            self.assertEqual(row["detail_attempts"], 0)
            self.assertIsNone(row["detail_last_attempt_at"])
            self.assertEqual(row["refresh_attempts"], 0)
            self.assertIsNone(row["refresh_last_attempt_at"])
            # Idempotent re-init
            migrated.init_schema()

    def test_get_round_statuses_and_single_status(self) -> None:
        self.assertEqual(self.db.get_round_statuses("taobao", []), {})
        self.assertIsNone(self.db.get_round_status("taobao", "missing"))

        self.db.upsert_list_item(
            RawAuctionItem(
                platform="taobao",
                item_id="tb_1",
                url="https://sf-item.taobao.com/sf_item/tb_1.htm",
                title="福州市鼓楼区测试花园1幢101室",
                province="福建",
                city="福州",
                status="sold",
            )
        )
        self.db.upsert_list_item(
            RawAuctionItem(
                platform="taobao",
                item_id="tb_2",
                url="https://sf-item.taobao.com/sf_item/tb_2.htm",
                title="福州市鼓楼区测试花园1幢201室",
                province="福建",
                city="福州",
                status="failed",
            )
        )
        self.db.upsert_list_item(
            RawAuctionItem(
                platform="jd",
                item_id="tb_1",
                url="https://paimai.jd.com/tb_1",
                title="福州市鼓楼区测试花园2幢101室",
                province="福建",
                city="福州",
                status="ongoing",
            )
        )

        batch = self.db.get_round_statuses("taobao", ["tb_1", "tb_2", "tb_999", "tb_1"])
        self.assertEqual(batch, {"tb_1": "sold", "tb_2": "failed"})
        self.assertEqual(self.db.get_round_status("taobao", "tb_1"), "sold")
        self.assertEqual(self.db.get_round_status("jd", "tb_1"), "ongoing")

    def test_list_rounds_needing_detail_filters_and_backoff(self) -> None:
        now = datetime.now(CN_TZ)
        old_dt = (now - timedelta(days=900)).strftime("%Y-%m-%d %H:%M:%S")
        recent_dt = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        min_date = (now - timedelta(days=730)).strftime("%Y-%m-%d %H:%M:%S")

        items = [
            RawAuctionItem(
                platform="taobao",
                item_id="d_ongoing",
                url="https://sf-item.taobao.com/sf_item/1.htm",
                title="福州市鼓楼区测试小区1幢101室",
                province="福建",
                city="福州",
                status="ongoing",
                end_time=old_dt,  # active items should bypass min_date
            ),
            RawAuctionItem(
                platform="taobao",
                item_id="d_upcoming",
                url="https://sf-item.taobao.com/sf_item/2.htm",
                title="福州市鼓楼区测试小区2幢101室",
                province="福建",
                city="福州",
                status="upcoming",
                start_time=recent_dt,
            ),
            RawAuctionItem(
                platform="taobao",
                item_id="d_sold_recent",
                url="https://sf-item.taobao.com/sf_item/3.htm",
                title="福州市鼓楼区测试小区3幢101室",
                province="福建",
                city="福州",
                status="sold",
                end_time=recent_dt,
            ),
            RawAuctionItem(
                platform="taobao",
                item_id="d_sold_old",
                url="https://sf-item.taobao.com/sf_item/4.htm",
                title="福州市鼓楼区测试小区4幢101室",
                province="福建",
                city="福州",
                status="sold",
                end_time=old_dt,
            ),
            RawAuctionItem(
                platform="taobao",
                item_id="d_sold_nodate",
                url="https://sf-item.taobao.com/sf_item/5.htm",
                title="福州市鼓楼区测试小区5幢101室",
                province="福建",
                city="福州",
                status="sold",
            ),
        ]
        for it in items:
            self.db.upsert_list_item(it)

        # Filter by allowed_statuses
        active_rows = self.db.list_rounds_needing_detail(
            "taobao",
            limit=20,
            allowed_statuses=["ongoing", "upcoming"],
        )
        self.assertEqual([r["item_id"] for r in active_rows], ["d_ongoing", "d_upcoming"])

        # Filter by min_date: excludes d_sold_old, keeps active + recent + null-date
        recent_rows = self.db.list_rounds_needing_detail(
            "taobao",
            limit=20,
            min_date=min_date,
        )
        recent_ids = {r["item_id"] for r in recent_rows}
        self.assertEqual(recent_ids, {"d_ongoing", "d_upcoming", "d_sold_recent", "d_sold_nodate"})

        # Backoff: record 1 attempt -> excluded during cooldown_hours=12, included when cooldown_hours=0
        self.db.record_detail_attempt("taobao", "d_ongoing")
        rows_cooling = self.db.list_rounds_needing_detail(
            "taobao",
            limit=20,
            allowed_statuses=["ongoing", "upcoming"],
            max_attempts=2,
            cooldown_hours=12,
        )
        self.assertEqual([r["item_id"] for r in rows_cooling], ["d_upcoming"])

        # Backdate attempt timestamp past cooldown -> becomes eligible again
        past_attempt = (now - timedelta(hours=13)).strftime("%Y-%m-%d %H:%M:%S")
        self.db.conn.execute(
            "UPDATE auction_rounds SET detail_last_attempt_at = ? WHERE item_id = 'd_ongoing'",
            (past_attempt,),
        )
        self.db.conn.commit()
        rows_ready = self.db.list_rounds_needing_detail(
            "taobao",
            limit=20,
            allowed_statuses=["ongoing", "upcoming"],
            max_attempts=2,
            cooldown_hours=12,
        )
        self.assertEqual([r["item_id"] for r in rows_ready], ["d_ongoing", "d_upcoming"])

        # Second attempt hits max_attempts=2 -> permanently excluded even without cooldown
        self.db.record_detail_attempt("taobao", "d_ongoing")
        rows_capped = self.db.list_rounds_needing_detail(
            "taobao",
            limit=20,
            allowed_statuses=["ongoing", "upcoming"],
            max_attempts=2,
            cooldown_hours=0,
        )
        self.assertEqual([r["item_id"] for r in rows_capped], ["d_upcoming"])

    def test_list_rounds_needing_ended_refresh_filters_and_backoff(self) -> None:
        now = datetime.now(CN_TZ)
        recent_end = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        stale_end = (now - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
        future_end = (now + timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        min_end_time = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

        for item_id, status, end_t in [
            ("r_recent_ended", "ended", recent_end),
            ("r_stale_ended", "ended", stale_end),
            ("r_future_ongoing", "ongoing", future_end),
            ("r_recent_sold", "sold", recent_end),
        ]:
            self.db.upsert_list_item(
                RawAuctionItem(
                    platform="taobao",
                    item_id=item_id,
                    url=f"https://sf-item.taobao.com/sf_item/{item_id}.htm",
                    title=f"福州市鼓楼区测试小区{item_id}101室",
                    province="福建",
                    city="福州",
                    status=status,
                    end_time=end_t,
                )
            )

        rows = self.db.list_rounds_needing_ended_refresh(
            "taobao",
            limit=20,
            min_end_time=min_end_time,
            max_attempts=2,
            cooldown_hours=12,
        )
        self.assertEqual([r["item_id"] for r in rows], ["r_recent_ended"])

        # Record 1 attempt -> hidden by cooldown
        self.db.record_refresh_attempt("taobao", "r_recent_ended")
        self.assertEqual(
            self.db.list_rounds_needing_ended_refresh(
                "taobao",
                limit=20,
                min_end_time=min_end_time,
                max_attempts=2,
                cooldown_hours=12,
            ),
            [],
        )

        # Past cooldown -> reappears
        old_attempt = (now - timedelta(hours=14)).strftime("%Y-%m-%d %H:%M:%S")
        self.db.conn.execute(
            "UPDATE auction_rounds SET refresh_last_attempt_at = ? WHERE item_id = 'r_recent_ended'",
            (old_attempt,),
        )
        self.db.conn.commit()
        rows_after_cd = self.db.list_rounds_needing_ended_refresh(
            "taobao",
            limit=20,
            min_end_time=min_end_time,
            max_attempts=2,
            cooldown_hours=12,
        )
        self.assertEqual([r["item_id"] for r in rows_after_cd], ["r_recent_ended"])

        # Second attempt reaches max_attempts=2 -> excluded even with cooldown_hours=0
        self.db.record_refresh_attempt("taobao", "r_recent_ended")
        self.assertEqual(
            self.db.list_rounds_needing_ended_refresh(
                "taobao",
                limit=20,
                min_end_time=min_end_time,
                max_attempts=2,
                cooldown_hours=0,
            ),
            [],
        )

    def test_cleanup_stale_data_dry_run_and_execute(self) -> None:
        min_date = "2024-11-21 00:00:00"

        # 1. Expired residential (2015) -> expired only, orphan property
        self.db.upsert_list_item(
            RawAuctionItem(
                platform="taobao",
                item_id="old_res",
                url="https://sf-item.taobao.com/sf_item/1.htm",
                title="福州市鼓楼区温泉花园1幢101室",
                province="福建",
                city="福州",
                status="sold",
                end_time="2015-05-01 10:00:00",
                category_hint="50025969",
            )
        )
        # 2. Expired + non-residential (2015 parking) -> in BOTH sets, counted once in union, orphan property
        self.db.upsert_list_item(
            RawAuctionItem(
                platform="taobao",
                item_id="old_park",
                url="https://sf-item.taobao.com/sf_item/2.htm",
                title="福州市晋安区和声花园地下一层05车位",
                province="福建",
                city="福州",
                status="failed",
                end_time="2015-06-01 10:00:00",
                category_hint="50025969",
            )
        )
        # 3a. Recent non-residential (parking) -> non-target only, orphan property
        self.db.upsert_list_item(
            RawAuctionItem(
                platform="jd",
                item_id="recent_park",
                url="https://paimai.jd.com/3",
                title="福州市晋安区鲁能花园地下1层0164车位",
                province="福建",
                city="福州",
                status="ongoing",
                end_time="2026-12-01 10:00:00",
                category_hint="12728",
            )
        )
        # 3b. Recent misclassified as residential in legacy DB (复式办公) -> caught by classify_property_type!
        p_office_id, _, _ = self.db.upsert_list_item(
            RawAuctionItem(
                platform="jd",
                item_id="recent_misclassified_office",
                url="https://paimai.jd.com/4",
                title="福州市马尾区信通观澜商务中心30#楼1层01复式办公",
                province="福建",
                city="福州",
                status="ongoing",
                end_time="2026-12-01 10:00:00",
                category_hint="12728",
            )
        )
        self.db.conn.execute(
            "UPDATE properties SET property_type = 'residential' WHERE id = ?",
            (p_office_id,),
        )
        self.db.conn.commit()

        # 3c. Detail-fetched property already classified commercial -> preserve non-residential classification
        p_comm_id, _, _ = self.db.upsert_list_item(
            RawAuctionItem(
                platform="jd",
                item_id="recent_fetched_comm",
                url="https://paimai.jd.com/5",
                title="福州市鼓楼区五四路某大厦808室",
                province="福建",
                city="福州",
                status="ongoing",
                end_time="2026-12-01 10:00:00",
                category_hint="12728",
            )
        )
        self.db.apply_detail(
            RawAuctionDetail(
                platform="jd",
                item_id="recent_fetched_comm",
                url="https://paimai.jd.com/5",
                notice_text="标的物说明",
            )
        )
        self.db.conn.execute(
            "UPDATE properties SET property_type = 'commercial' WHERE id = ?",
            (p_comm_id,),
        )
        self.db.conn.commit()

        # 4. Shared residential property: round 1 expired (deleted), round 2 recent (kept) -> property NOT orphan
        self.db.upsert_list_item(
            RawAuctionItem(
                platform="taobao",
                item_id="shared_old",
                url="https://sf-item.taobao.com/sf_item/6.htm",
                title="【一拍】福州市仓山区橘园洲花园8幢902室",
                province="福建",
                city="福州",
                status="failed",
                end_time="2016-01-01 10:00:00",
                category_hint="50025969",
            )
        )
        self.db.upsert_list_item(
            RawAuctionItem(
                platform="taobao",
                item_id="shared_new",
                url="https://sf-item.taobao.com/sf_item/7.htm",
                title="【二拍】福州市仓山区橘园洲花园8幢902室",
                province="福建",
                city="福州",
                status="sold",
                end_time="2026-08-01 10:00:00",
                category_hint="50025969",
            )
        )

        # 5. Kept recent residential with captcha-polluted notice_text -> should reset
        self.db.upsert_list_item(
            RawAuctionItem(
                platform="taobao",
                item_id="kept_captcha",
                url="https://sf-item.taobao.com/sf_item/8.htm",
                title="福州市鼓楼区左海天骐园9#楼101单元",
                province="福建",
                city="福州",
                status="ongoing",
                end_time="2026-12-10 10:00:00",
                category_hint="50025969",
            )
        )
        self.db.apply_detail(
            RawAuctionDetail(
                platform="taobao",
                item_id="kept_captcha",
                url="https://sf-item.taobao.com/sf_item/8.htm",
                notice_text="请按住滑块拖动到最右边通过验证以确保正常访问",
                survey_text="污染内容",
            )
        )
        self.db.record_detail_attempt("taobao", "kept_captcha")

        # 6. Kept recent residential with status='unknown' + revocation in raw_json -> should fix to 'withdrawn'
        self.db.upsert_list_item(
            RawAuctionItem(
                platform="taobao",
                item_id="kept_revocation",
                url="https://sf-item.taobao.com/sf_item/9.htm",
                title="福州市马尾区名城港湾23#楼501室",
                province="福建",
                city="福州",
                status="unknown",
                end_time="2026-09-01 10:00:00",
                category_hint="50025969",
                raw_payload={"id": "9", "status": "revocation"},
            )
        )
        # Force status='unknown' in case normalize_status is later updated to map revocation
        self.db.conn.execute(
            "UPDATE auction_rounds SET status = 'unknown' WHERE item_id = 'kept_revocation'"
        )
        self.db.conn.commit()

        # 7. Active upcoming residential with old start_time -> MUST NOT be treated as expired
        self.db.upsert_list_item(
            RawAuctionItem(
                platform="taobao",
                item_id="active_old_ts",
                url="https://sf-item.taobao.com/sf_item/10.htm",
                title="福州市鼓楼区杨桥路某花园3幢601室",
                province="福建",
                city="福州",
                status="upcoming",
                start_time="2016-01-01 10:00:00",
                category_hint="50025969",
            )
        )

        # Also add captcha + revocation on expired round old_res -> must NOT be counted in kept resets/fixes
        self.db.conn.execute(
            "UPDATE auction_rounds SET notice_text = '拖动下方滑块验证', raw_json = '{\"status\":\"revocation\"}' "
            "WHERE item_id = 'old_res'"
        )
        self.db.conn.commit()

        before_rounds = self.db.conn.execute("SELECT COUNT(*) FROM auction_rounds").fetchone()[0]
        before_props = self.db.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]

        # Dry run must not mutate DB
        preview = self.db.cleanup_stale_data(
            min_date=min_date,
            allowed_property_types=["residential"],
            reset_captcha_details=True,
            dry_run=True,
        )
        self.assertEqual(
            preview,
            {
                "expired_rounds": 3,  # old_res, old_park, shared_old
                "non_target_type_rounds": 4,  # old_park, recent_park, recent_misclassified_office, recent_fetched_comm
                "total_delete_rounds": 6,  # union of 3 and 4 with 1 overlap (old_park)
                "orphan_properties": 5,  # all deleted except shared_old's property
                "captcha_reset_rounds": 1,  # kept_captcha
                "revocation_fixed_rounds": 1,  # kept_revocation
                "dry_run": True,
            },
        )
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) FROM auction_rounds").fetchone()[0],
            before_rounds,
        )
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0],
            before_props,
        )

        # Dry run with reset_captcha_details=False reports 0 captcha resets
        preview_no_captcha = self.db.cleanup_stale_data(
            min_date=min_date,
            allowed_property_types=["residential"],
            reset_captcha_details=False,
            dry_run=True,
        )
        self.assertEqual(preview_no_captcha["captcha_reset_rounds"], 0)

        # Execute real cleanup
        executed = self.db.cleanup_stale_data(
            min_date=min_date,
            allowed_property_types=["residential"],
            reset_captcha_details=True,
            dry_run=False,
        )
        self.assertEqual(executed["dry_run"], False)
        self.assertEqual(executed["total_delete_rounds"], 6)
        self.assertEqual(executed["orphan_properties"], 5)
        self.assertEqual(executed["captcha_reset_rounds"], 1)
        self.assertEqual(executed["revocation_fixed_rounds"], 1)

        remaining_ids = {
            r["item_id"]
            for r in self.db.conn.execute("SELECT item_id FROM auction_rounds").fetchall()
        }
        self.assertEqual(
            remaining_ids,
            {"shared_new", "kept_captcha", "kept_revocation", "active_old_ts"},
        )
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0],
            before_props - 5,
        )

        cap_row = self.db.conn.execute(
            "SELECT detail_fetched_at, notice_text, survey_text, detail_attempts, detail_last_attempt_at "
            "FROM auction_rounds WHERE item_id = 'kept_captcha'"
        ).fetchone()
        self.assertIsNone(cap_row["detail_fetched_at"])
        self.assertEqual(cap_row["notice_text"], "")
        self.assertEqual(cap_row["survey_text"], "")
        self.assertEqual(cap_row["detail_attempts"], 0)
        self.assertIsNone(cap_row["detail_last_attempt_at"])

        self.assertEqual(self.db.get_round_status("taobao", "kept_revocation"), "withdrawn")

    def test_config_normalize_statuses_defaults_and_validation(self) -> None:
        self.assertEqual(
            VALID_AUCTION_STATUSES,
            ("upcoming", "ongoing", "sold", "failed", "withdrawn", "suspended", "ended", "unknown"),
        )
        self.assertEqual(normalize_statuses(None), [])
        self.assertEqual(normalize_statuses(""), [])
        self.assertEqual(normalize_statuses("all"), [])
        self.assertEqual(normalize_statuses(["*"]), [])
        self.assertEqual(
            normalize_statuses("ongoing, UPCOMING, ongoing"),
            ["ongoing", "upcoming"],
        )
        self.assertEqual(
            normalize_statuses(["sold", "failed", "sold"]),
            ["sold", "failed"],
        )
        with self.assertRaises(ValueError):
            normalize_statuses(["ongoing", "bad_status"])
        with self.assertRaises(ValueError):
            normalize_statuses(123)  # type: ignore[arg-type]

        # Backward-compatible ProviderConfig() and SyncSummary defaults
        default_provider = ProviderConfig()
        self.assertEqual(default_provider.lookback_days, 0)
        self.assertEqual(default_provider.enrich_statuses, [])
        self.assertTrue(default_provider.stop_on_known_ended_page)
        self.assertEqual(default_provider.max_detail_attempts, 3)
        self.assertEqual(default_provider.max_refresh_attempts, 3)
        self.assertEqual(default_provider.retry_cooldown_hours, 12)
        self.assertEqual(default_provider.ended_refresh_window_days, 30)

        summary = SyncSummary(run_id=1, provider="taobao", region_name="福州", status="success")
        self.assertEqual(summary.items_skipped_old, 0)
        self.assertEqual(summary.items_skipped_existing, 0)

        # load_config valid & invalid values
        valid_cfg_path = Path(self.tmpdir.name) / "valid.toml"
        valid_cfg_path.write_text(
            """
[provider]
lookback_days = 730
enrich_statuses = ["ongoing", "upcoming"]
stop_on_known_ended_page = false
max_detail_attempts = 5
max_refresh_attempts = 4
retry_cooldown_hours = 6
ended_refresh_window_days = 15
""".strip(),
            encoding="utf-8",
        )
        cfg = load_config(valid_cfg_path)
        self.assertEqual(cfg.provider.lookback_days, 730)
        self.assertEqual(cfg.provider.enrich_statuses, ["ongoing", "upcoming"])
        self.assertFalse(cfg.provider.stop_on_known_ended_page)
        self.assertEqual(cfg.provider.max_detail_attempts, 5)
        self.assertEqual(cfg.provider.max_refresh_attempts, 4)
        self.assertEqual(cfg.provider.retry_cooldown_hours, 6)
        self.assertEqual(cfg.provider.ended_refresh_window_days, 15)

        for bad_snippet in [
            "lookback_days = -1",
            "max_detail_attempts = 0",
            "max_refresh_attempts = 0",
            "retry_cooldown_hours = -2",
            "ended_refresh_window_days = -5",
            'enrich_statuses = ["not_a_status"]',
        ]:
            bad_path = Path(self.tmpdir.name) / "bad.toml"
            bad_path.write_text(f"[provider]\n{bad_snippet}\n", encoding="utf-8")
            with self.assertRaises(ValueError, msg=f"Expected ValueError for {bad_snippet}"):
                load_config(bad_path)

    def test_reset_checkpoints_escapes_like_and_readonly_avoids_immutable(self) -> None:
        fresh_db = Path(self.tmpdir.name) / "ro_fresh.db"
        with AuctionDB(fresh_db) as db:
            db.save_checkpoint("taobao", "福州市-住宅", "ended:2", next_page=4, backfill_completed=False, last_status="success")
            db.save_checkpoint("taobao", "100%_测试", "ended:2", next_page=2, backfill_completed=False, last_status="success")

            # '%' literal must NOT delete '福州市-住宅'
            deleted = db.reset_checkpoints(region_name="100%")
            self.assertEqual(deleted, 1)
            cp = db.get_checkpoint("taobao", "福州市-住宅", "ended:2")
            self.assertIsNotNone(cp)
            self.assertEqual(cp["next_page"], 4)

        # Even when -wal is absent after clean close, read_only must NOT use immutable=1 and must see new writes
        self.assertFalse(Path(f"{fresh_db}-wal").exists())
        with AuctionDB(fresh_db, read_only=True) as ro_db:
            with AuctionDB(fresh_db) as rw_db:
                rw_db.save_checkpoint("taobao", "福州市-住宅", "ended:2", next_page=9, backfill_completed=True, last_status="success")
            cp_after = ro_db.get_checkpoint("taobao", "福州市-住宅", "ended:2")
            self.assertIsNotNone(cp_after)
            self.assertEqual(cp_after["next_page"], 9)


if __name__ == "__main__":
    unittest.main()
