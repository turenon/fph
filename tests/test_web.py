from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from fph.config import AppConfig, StorageConfig
from fph.db import AuctionDB
from fph.models import RawAuctionDetail, RawAuctionItem, SyncSummary
from fph.web import create_web_server


class WebDashboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "web_test.db"

        with AuctionDB(self.db_path) as db:
            # Property 1 (Fuzhou Gulou): Round 1 failed -> Round 2 sold (enriched + 2 rounds)
            db.upsert_list_item(
                RawAuctionItem(
                    platform="taobao",
                    item_id="w_101_r1",
                    url="https://sf-item.taobao.com/sf_item/w_101_r1.htm",
                    title="【一拍】福州市鼓楼区温泉花园2幢301室住宅",
                    province="福建",
                    city="福州",
                    stage="一拍",
                    status="failed",
                    initial_price=2_000_000,
                    current_price=2_000_000,
                    consult_price=2_500_000,
                    bid_count=0,
                    watch_count=120,
                    end_time="2026-10-10 10:00:00",
                    category_hint="50025969",
                )
            )
            _, r2_id, _ = db.upsert_list_item(
                RawAuctionItem(
                    platform="taobao",
                    item_id="w_101_r2",
                    url="https://sf-item.taobao.com/sf_item/w_101_r2.htm",
                    title="【二拍】福州市鼓楼区温泉花园2#楼301室不动产",
                    province="福建",
                    city="福州",
                    stage="二拍",
                    status="ongoing",
                    initial_price=1_600_000,
                    current_price=1_600_000,
                    consult_price=2_500_000,
                    bid_count=0,
                    apply_count=1,
                    watch_count=350,
                    end_time="2026-11-28 10:00:00",
                    category_hint="50025969",
                )
            )
            self.r2_id = r2_id
            db.apply_detail(
                RawAuctionDetail(
                    platform="taobao",
                    item_id="w_101_r2",
                    url="https://sf-item.taobao.com/sf_item/w_101_r2.htm",
                    status="sold",
                    current_price=1_750_000,
                    bid_count=4,
                    court_name="福州市鼓楼区人民法院",
                    notice_text="建筑面积100平方米，已腾空交付。",
                    survey_text="南北通透，精装修。",
                )
            )

            # Property 2 (Fuzhou Cangshan): Active bargain candidate
            db.upsert_list_item(
                RawAuctionItem(
                    platform="taobao",
                    item_id="w_202_r2",
                    url="https://sf-item.taobao.com/sf_item/w_202_r2.htm",
                    title="【二拍】福州市仓山区橘园洲花园5幢602室住宅",
                    province="福建",
                    city="福州",
                    stage="二拍",
                    status="upcoming",
                    initial_price=1_400_000,
                    current_price=1_400_000,
                    consult_price=2_000_000,
                    apply_count=0,
                    bid_count=0,
                    watch_count=200,
                    end_time="2026-12-01 10:00:00",
                    category_hint="50025969",
                )
            )

            # Property 3 (Commercial): non-residential
            db.upsert_list_item(
                RawAuctionItem(
                    platform="taobao",
                    item_id="w_303_comm",
                    url="https://sf-item.taobao.com/sf_item/w_303_comm.htm",
                    title="福州市台江区世茂国际中心1号楼写字楼商铺",
                    province="福建",
                    city="福州",
                    stage="一拍",
                    status="failed",
                    initial_price=3_000_000,
                    current_price=3_000_000,
                    consult_price=3_200_000,
                    watch_count=90,
                    end_time="2026-10-20 10:00:00",
                    category_hint="50025969",
                )
            )

            # Property 4 (JD residential, Longyan): tests multi-platform overview & filtering
            db.upsert_list_item(
                RawAuctionItem(
                    platform="jd",
                    item_id="w_404_jd",
                    url="https://paimai.jd.com/w_404_jd",
                    title="龙岩市新罗区莲东小区10幢201室住宅",
                    province="福建",
                    city="龙岩",
                    district="新罗区",
                    stage="一拍",
                    status="withdrawn",
                    initial_price=900_000,
                    current_price=900_000,
                    consult_price=1_100_000,
                    watch_count=60,
                    end_time="2026-10-25 10:00:00",
                    category_hint="12728",
                    community_name="莲东小区",
                    area_sqm=115.0,
                )
            )

            run_id = db.start_sync_run("taobao", "福州市-住宅")
            db.finish_sync_run(
                SyncSummary(
                    run_id=run_id,
                    provider="taobao",
                    region_name="福州市-住宅",
                    status="success",
                    pages_fetched=1,
                    items_seen=4,
                    properties_created=3,
                    rounds_upserted=4,
                    details_enriched=1,
                    ended_refreshed=0,
                    message="ok",
                )
            )

        cfg = AppConfig(storage=StorageConfig(db_path=self.db_path))
        self.server = create_web_server(cfg, host="127.0.0.1", port=0)
        port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmpdir.cleanup()

    def _get_json(self, path: str) -> dict:
        with urlopen(f"{self.base_url}{path}", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            return json.loads(resp.read().decode("utf-8"))

    def test_html_and_overview(self) -> None:
        with urlopen(f"{self.base_url}/", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            html = resp.read().decode("utf-8")
            self.assertIn("FPH 法拍房数据看板", html)

        overview = self._get_json("/api/overview")
        self.assertEqual(overview["counts"]["properties_total"], 4)
        self.assertEqual(overview["counts"]["rounds_total"], 5)
        self.assertEqual(overview["counts"]["residential_rounds"], 4)
        self.assertEqual(overview["counts"]["active_rounds"], 1)
        self.assertEqual(overview["counts"]["sold_rounds"], 1)
        self.assertEqual(overview["counts"]["failed_rounds"], 2)
        self.assertEqual(overview["counts"]["enriched_rounds"], 1)
        self.assertIn("福州", overview["cities"])
        plat_map = {p["platform"]: p["count"] for p in overview["platforms"]}
        self.assertEqual(plat_map["taobao"], 4)
        self.assertEqual(plat_map["jd"], 1)

    def test_items_filter_sort_pagination_and_detail(self) -> None:
        all_items = self._get_json("/api/items?page=1&page_size=2&sort=watch_desc")
        self.assertEqual(all_items["total"], 5)
        self.assertEqual(len(all_items["items"]), 2)
        self.assertEqual(all_items["items"][0]["item_id"], "w_101_r2")

        jd_items = self._get_json("/api/items?platform=jd")
        self.assertEqual(jd_items["total"], 1)
        self.assertEqual(jd_items["items"][0]["item_id"], "w_404_jd")
        self.assertEqual(jd_items["items"][0]["area_sqm"], 115.0)

        comm = self._get_json("/api/items?property_type=commercial")
        self.assertEqual(comm["total"], 1)
        self.assertEqual(comm["items"][0]["item_id"], "w_303_comm")

        enriched = self._get_json("/api/items?only_enriched=1&q=%E6%B8%A9%E6%B3%89")
        self.assertEqual(enriched["total"], 1)
        self.assertEqual(enriched["items"][0]["item_id"], "w_101_r2")

        active = self._get_json("/api/items?status=active")
        self.assertEqual(active["total"], 1)
        self.assertEqual(active["items"][0]["item_id"], "w_202_r2")

        detail = self._get_json(f"/api/items/{self.r2_id}")
        self.assertEqual(detail["item_id"], "w_101_r2")
        self.assertEqual(len(detail["related_rounds"]), 2)
        self.assertGreaterEqual(len(detail["snapshots"]), 2)
        self.assertIn("100平方米", detail["notice_text"])
        self.assertIn("南北通透", detail["survey_text"])

        with self.assertRaises(HTTPError) as ctx:
            urlopen(f"{self.base_url}/api/items/999999", timeout=5)
        self.assertEqual(ctx.exception.code, 404)

    def test_stats_bargains_logs_and_export(self) -> None:
        stats = self._get_json("/api/stats?city=%E7%A6%8F%E5%B7%9E")
        self.assertEqual(len(stats["rows"]), 1)
        self.assertEqual(stats["rows"][0]["sold_count"], 1)
        self.assertEqual(stats["rows"][0]["failed_count"], 1)

        bargains = self._get_json("/api/bargains?max_discount=0.75")
        self.assertEqual(len(bargains["rows"]), 1)
        self.assertEqual(bargains["rows"][0]["district"], "仓山区")

        logs = self._get_json("/api/sync-runs")
        self.assertEqual(len(logs["rows"]), 1)
        self.assertEqual(logs["rows"][0]["status"], "success")

        with urlopen(f"{self.base_url}/api/export?dataset=all&format=csv", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            raw = resp.read()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            text = raw.decode("utf-8-sig")
            self.assertIn("w_101_r2", text)
            self.assertIn("w_303_comm", text)


if __name__ == "__main__":
    unittest.main()
