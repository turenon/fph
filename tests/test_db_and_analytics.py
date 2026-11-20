from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fph.analytics import export_dataset, find_bargain_candidates, get_market_summary
from fph.db import AuctionDB
from fph.models import RawAuctionDetail, RawAuctionItem


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


if __name__ == "__main__":
    unittest.main()
