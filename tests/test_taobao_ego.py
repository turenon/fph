from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fph.config import AppConfig, ProviderConfig, RegionConfig, StorageConfig
from fph.db import AuctionDB
from fph.pipeline import SyncPipeline
from fph.providers.base import AuthRequiredError
from fph.providers.taobao_ego import MARKER_END, MARKER_START, TaobaoEgoProvider
from fph.providers.taobao_urls import build_list_url


class TaobaoEgoAndPipelineTest(unittest.TestCase):
    def test_url_builder_location_code_and_gbk_fallback(self) -> None:
        fz = RegionConfig(name="福州市-住宅", province="福建", city="福州", location_code="350100")
        url_fz = build_list_url(fz, st_param="1", page=2)
        self.assertIn("sf.taobao.com/list/50025969.htm", url_fz)
        self.assertIn("location_code=350100", url_fz)
        self.assertIn("st_param=1", url_fz)
        self.assertIn("page=2", url_fz)

        ly_no_code = RegionConfig(name="龙岩", province="福建", city="龙岩", location_code="")
        url_ly = build_list_url(ly_no_code, st_param="2", page=1)
        self.assertIn("sf.taobao.com/item_list.htm", url_ly)
        self.assertIn("city=%C1%FA%D1%D2", url_ly)  # GBK for 龙岩

    def test_auth_required_is_detected_and_recorded(self) -> None:
        def fake_runner(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
            payload = {
                "authRequired": True,
                "reason": "检测到登录页",
                "currentUrl": "https://login.taobao.com/member/login.jhtml",
            }
            return 0, f"info log\n{MARKER_START}{json.dumps(payload, ensure_ascii=False)}{MARKER_END}\n", ""

        provider = TaobaoEgoProvider(ProviderConfig(task_space="test-space"), runner=fake_runner)
        region = RegionConfig(name="福州市-住宅", province="福建", city="福州", location_code="350100")

        with self.assertRaises(AuthRequiredError) as ctx:
            provider.fetch_list_page(region, status_scope="1", page=1)
        self.assertEqual(ctx.exception.task_space, "test-space")

    def test_end_to_end_pipeline_with_mock_ego_runner(self) -> None:
        calls: list[str] = []

        def fake_runner(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
            calls.append(stdin_text)
            if "sf_item/888001" in stdin_text:
                payload = {
                    "authRequired": False,
                    "currentUrl": "https://sf-item.taobao.com/sf_item/888001.htm",
                    "title": "【二拍】龙岩市新罗区西陂街道华莲花园6幢801室",
                    "status": "拍卖已成交",
                    "currentPrice": "¥76.5万",
                    "initialPrice": "¥72万",
                    "consultPrice": "¥105万",
                    "bidCount": "4",
                    "applyCount": "3",
                    "courtName": "龙岩市新罗区人民法院",
                    "noticeText": "标的物建筑面积110.5平方米，已腾空交付。",
                    "surveyText": "",
                }
            else:
                payload = {
                    "authRequired": False,
                    "currentUrl": "https://sf.taobao.com/list/50025969.htm?location_code=350800",
                    "extractor": "sf-item-list-data",
                    "totalPages": 1,
                    "hasNext": False,
                    "items": [
                        {
                            "id": "888001",
                            "title": "【二拍】龙岩市新罗区西陂街道华莲花园6幢801室",
                            "url": "//sf-item.taobao.com/sf_item/888001.htm",
                            "status": "ended",
                            "stage": "二拍",
                            "initialPrice": 720000,
                            "currentPrice": 765000,
                            "consultPrice": 1050000,
                            "bidCount": 4,
                            "applyCount": 3,
                            "start": "2026-11-10 10:00:00",
                            "end": "2026-11-11 10:00:00",
                        }
                    ],
                }
            return 0, f"{MARKER_START}{json.dumps(payload, ensure_ascii=False)}{MARKER_END}", ""

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = AppConfig(
                storage=StorageConfig(db_path=Path(tmpdir) / "fph.db"),
                provider=ProviderConfig(min_delay_seconds=0, max_delay_seconds=0, max_pages_per_query=1),
                regions=[
                    RegionConfig(
                        name="龙岩市-住宅",
                        province="福建",
                        city="龙岩",
                        location_code="350800",
                        status_scopes=["ended"],
                    )
                ],
            )
            with AuctionDB(cfg.storage.db_path) as db:
                provider = TaobaoEgoProvider(cfg.provider, runner=fake_runner)
                pipeline = SyncPipeline(cfg, db, provider, sleep_fn=lambda _: None)
                summaries = pipeline.run_full_cycle(enrich_details=True, refresh_ended=False)

                self.assertEqual(len(summaries), 1)
                self.assertEqual(summaries[0].status, "success")
                self.assertEqual(summaries[0].items_seen, 1)
                self.assertEqual(summaries[0].details_enriched, 1)

                row = db.conn.execute("SELECT * FROM v_auction_full WHERE item_id = '888001'").fetchone()
                self.assertEqual(row["city"], "龙岩")
                self.assertEqual(row["district"], "新罗区")
                self.assertEqual(row["status"], "sold")
                self.assertEqual(row["area_sqm"], 110.5)
                self.assertEqual(row["court_name"], "龙岩市新罗区人民法院")


if __name__ == "__main__":
    unittest.main()
