from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fph.config import AppConfig, ProviderConfig, RegionConfig, StorageConfig
from fph.db import AuctionDB
from fph.pipeline import SyncPipeline
from fph.providers.base import AuthRequiredError, ProviderError
from fph.providers.ego_runtime import MARKER_END, MARKER_START
from fph.providers.jd_ego import JdEgoProvider
from fph.providers.jd_urls import build_jd_list_url, expand_jd_status_queries
from fph.providers.taobao_ego import TaobaoEgoProvider


class JdEgoAndMultiPlatformTest(unittest.TestCase):
    def test_jd_url_builder_and_status_expansion(self) -> None:
        fz = RegionConfig(
            name="福州市-住宅-京东",
            provider="jd_ego",
            province="福建",
            city="福州",
            jd_province_id="16",
            jd_city_id="1303",
            jd_category_id="12728",
        )
        url_ongoing = build_jd_list_url(fz, st_param="101", page=1)
        self.assertIn("https://pmsearch.jd.com/", url_ongoing)
        self.assertIn("publishSource=7", url_ongoing)
        self.assertIn("spo_multiProvinceIds=16", url_ongoing)
        self.assertIn("spo_multiCityIds=1303", url_ongoing)
        self.assertIn("spo_multiThirdCateIds=12728", url_ongoing)
        self.assertIn("spo_multiStatus=101", url_ongoing)

        # Auto-resolve province/city/default category by Chinese names
        ly = RegionConfig(name="龙岩住宅", province="福建省", city="龙岩市", category="50025969")
        url_ly = build_jd_list_url(ly, st_param="102")
        self.assertIn("spo_multiProvinceIds=16", url_ly)
        self.assertIn("spo_multiCityIds=1362", url_ly)
        self.assertIn("spo_multiThirdCateIds=12728", url_ly)
        self.assertIn("spo_multiStatus=102", url_ly)

        # custom_url preserves base query and overrides spo_multiStatus
        custom = RegionConfig(
            name="自定义京东",
            province="福建",
            city="福州",
            custom_url="https://pmsearch.jd.com/?pageParam=pc_search_judicature&publishSource=7&spo_multiCityIds=1303&paimaiStatus=1",
        )
        url_custom = build_jd_list_url(custom, st_param="105")
        self.assertIn("spo_multiCityIds=1303", url_custom)
        self.assertIn("spo_multiStatus=105", url_custom)
        self.assertNotIn("paimaiStatus=", url_custom)

        self.assertEqual(expand_jd_status_queries("active"), [("ongoing", "101"), ("upcoming", "102")])
        self.assertEqual(expand_jd_status_queries("ended"), [("ended", "105")])

    def test_jd_provider_list_detail_pagination_and_auth(self) -> None:
        captured_scripts: list[str] = []

        def fake_runner(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
            captured_scripts.append(stdin_text)
            if "paimai.jd.com/777001" in stdin_text:
                payload = {
                    "authRequired": False,
                    "currentUrl": "https://paimai.jd.com/777001",
                    "title": "【二拍】福州市鼓楼区五四路温泉花园2幢301室住宅",
                    "status": "竞价成功",
                    "currentPrice": "¥180万",
                    "initialPrice": "¥160万",
                    "consultPrice": "¥240万",
                    "depositPrice": "¥20万",
                    "bidCount": "3",
                    "applyCount": "2",
                    "watchCount": "520",
                    "courtName": "福州市鼓楼区人民法院",
                    "address": "福州市鼓楼区五四路温泉花园2幢301室",
                    "areaSqm": "102.5",
                    "noticeText": "标的物建筑面积102.5平方米，已腾空交付。",
                    "surveyText": "南北通透，精装。",
                }
                return 0, "", f"{MARKER_START}{json.dumps(payload, ensure_ascii=False)}{MARKER_END}\n"

            payload = {
                "authRequired": False,
                "currentUrl": "https://pmsearch.jd.com/?publishSource=7&spo_multiCityIds=1303",
                "extractor": "react-fiber",
                "totalPages": 3,
                "hasNext": True,
                "items": [
                    {
                        "id": "777001",
                        "title": "福州市鼓楼区五四路温泉花园2幢301室住宅",
                        "url": "https://paimai.jd.com/777001",
                        "status": "正在进行",
                        "stage": "二拍",
                        "initialPrice": 1600000,
                        "currentPrice": 1650000,
                        "consultPrice": 2400000,
                        "bidCount": 2,
                        "watchCount": 480,
                        "start": 1762740000000,
                        "end": 1762826400000,
                        "areaSqm": 102.5,
                        "housePurpose": "住宅",
                        "communityName": "温泉花园",
                        "location": "福建 福州市 鼓楼区",
                    },
                    {
                        "id": "777002",
                        "title": "福州市仓山区某小区1幢101室住宅",
                        "url": "https://paimai.jd.com/777002",
                        "status": "正在进行",
                        "stage": "",
                        "initialPrice": 1200000,
                        "raw": {"data": {"labelSet": [1027, 1051, 1115]}},
                    },
                ],
            }
            return 0, "", f"{MARKER_START}{json.dumps(payload, ensure_ascii=False)}{MARKER_END}\n"

        provider = JdEgoProvider(ProviderConfig(task_space="jd-space"), runner=fake_runner)
        region = RegionConfig(name="福州市-住宅-京东", provider="jd_ego", province="福建", city="福州")

        page_res = provider.fetch_list_page(region, status_scope="101", page=2)
        self.assertEqual(page_res.extractor, "react-fiber")
        self.assertTrue(page_res.has_next)
        self.assertEqual(page_res.total_pages, 3)
        self.assertEqual(len(page_res.items), 2)
        item = page_res.items[0]
        self.assertEqual(item.platform, "jd")
        self.assertEqual(item.district, "鼓楼区")
        self.assertEqual(item.stage, "二拍")
        self.assertEqual(item.status, "ongoing")
        self.assertEqual(item.area_sqm, 102.5)
        self.assertEqual(item.purpose_hint, "住宅")
        self.assertEqual(item.community_name, "温泉花园")
        self.assertEqual(page_res.items[1].stage, "一拍")
        self.assertIn("housePurpose", captured_scripts[0])
        self.assertIn('"page": 2', captured_scripts[0])
        self.assertIn("goToTargetPage", captured_scripts[0])

        detail = provider.fetch_detail("777001", "https://paimai.jd.com/777001")
        self.assertEqual(detail.platform, "jd")
        self.assertEqual(detail.status, "sold")
        self.assertEqual(detail.current_price, 1_800_000.0)
        self.assertEqual(detail.area_sqm, 102.5)
        self.assertEqual(detail.court_name, "福州市鼓楼区人民法院")

        # Permission prompt / user control triggers AuthRequiredError
        def prompt_runner(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
            return 1, "", "Error: A browser permission prompt for local network access has appeared.\nThe user now controls this task space."

        prompt_provider = JdEgoProvider(ProviderConfig(task_space="jd-space"), runner=prompt_runner)
        with self.assertRaises(AuthRequiredError) as ctx:
            prompt_provider.fetch_list_page(region, status_scope="101", page=1)
        self.assertEqual(ctx.exception.task_space, "jd-space")

    def test_multi_platform_pipeline_dedup_filter_and_isolation(self) -> None:
        def taobao_runner(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
            payload = {
                "authRequired": False,
                "currentUrl": "https://sf.taobao.com/list/50025969.htm",
                "extractor": "sf-item-list-data",
                "totalPages": 1,
                "hasNext": False,
                "items": [
                    {
                        "id": "tb_9001",
                        "title": "【一拍】福州市鼓楼区温泉花园2幢301室住宅",
                        "url": "//sf-item.taobao.com/sf_item/tb_9001.htm",
                        "status": "ended",
                        "stage": "一拍",
                        "initialPrice": 2000000,
                        "currentPrice": 2000000,
                        "consultPrice": 2500000,
                        "bidCount": 0,
                    }
                ],
            }
            return 0, f"{MARKER_START}{json.dumps(payload, ensure_ascii=False)}{MARKER_END}", ""

        def jd_runner(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
            if "paimai.jd.com/jd_9002" in stdin_text:
                payload = {
                    "authRequired": False,
                    "currentUrl": "https://paimai.jd.com/jd_9002",
                    "title": "【二拍】福州市鼓楼区温泉花园2#楼301室",
                    "status": "即将开拍",
                    "initialPrice": "¥160万",
                    "consultPrice": "¥250万",
                    "courtName": "福州市鼓楼区人民法院",
                    "noticeText": "建筑面积100平方米，已腾空。",
                }
            else:
                payload = {
                    "authRequired": False,
                    "currentUrl": "https://pmsearch.jd.com/",
                    "extractor": "react-fiber",
                    "totalPages": 1,
                    "hasNext": False,
                    "items": [
                        {
                            "id": "jd_9002",
                            "title": "【二拍】福州市鼓楼区温泉花园2#楼301室",
                            "url": "https://paimai.jd.com/jd_9002",
                            "status": "即将开拍",
                            "stage": "二拍",
                            "initialPrice": 1600000,
                            "currentPrice": 1600000,
                            "consultPrice": 2500000,
                            "bidCount": 0,
                            "areaSqm": 100.0,
                            "communityName": "温泉花园",
                            "location": "福建 福州市 鼓楼区",
                        }
                    ],
                }
            return 0, "", f"{MARKER_START}{json.dumps(payload, ensure_ascii=False)}{MARKER_END}"

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = AppConfig(
                storage=StorageConfig(db_path=Path(tmpdir) / "multi.db"),
                provider=ProviderConfig(min_delay_seconds=0, max_delay_seconds=0, max_pages_per_query=1),
                regions=[
                    RegionConfig(
                        name="福州市-住宅",
                        provider="taobao_ego",
                        province="福建",
                        city="福州",
                        location_code="350100",
                        status_scopes=["ended"],
                    ),
                    RegionConfig(
                        name="福州市-住宅-京东",
                        provider="jd_ego",
                        province="福建",
                        city="福州",
                        jd_province_id="16",
                        jd_city_id="1303",
                        status_scopes=["upcoming"],
                    ),
                ],
            )
            with AuctionDB(cfg.storage.db_path) as db:
                tb_drv = TaobaoEgoProvider(cfg.provider, runner=taobao_runner)
                jd_drv = JdEgoProvider(cfg.provider, runner=jd_runner)
                pipeline = SyncPipeline(
                    cfg,
                    db,
                    providers={"taobao_ego": tb_drv, "jd_ego": jd_drv},
                    sleep_fn=lambda _: None,
                )

                # 1. Run Taobao only first
                tb_sums = pipeline.run_full_cycle(provider_filter="taobao", enrich_details=False, refresh_ended=False)
                self.assertEqual(len(tb_sums), 1)
                self.assertEqual(tb_sums[0].provider, "taobao")

                # 2. Run JD only with enrichment
                jd_sums = pipeline.run_full_cycle(provider_filter="jd", enrich_details=True, refresh_ended=False)
                self.assertEqual(len(jd_sums), 1)
                self.assertEqual(jd_sums[0].provider, "jd")
                self.assertEqual(jd_sums[0].properties_created, 0)  # Merged into same property!
                self.assertEqual(jd_sums[0].details_enriched, 1)

                prop_cnt = db.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
                round_cnt = db.conn.execute("SELECT COUNT(*) FROM auction_rounds").fetchone()[0]
                self.assertEqual(prop_cnt, 1)
                self.assertEqual(round_cnt, 2)

                # 3. Auth failure on Taobao does NOT block JD in combined cycle
                def blocked_tb_runner(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
                    payload = {"authRequired": True, "reason": "需要登录"}
                    return 0, f"{MARKER_START}{json.dumps(payload)}{MARKER_END}", ""

                pipeline2 = SyncPipeline(
                    cfg,
                    db,
                    providers={
                        "taobao_ego": TaobaoEgoProvider(cfg.provider, runner=blocked_tb_runner),
                        "jd_ego": jd_drv,
                    },
                    sleep_fn=lambda _: None,
                )
                both_sums = pipeline2.run_full_cycle(enrich_details=False, refresh_ended=False)
                self.assertEqual(len(both_sums), 2)
                by_plat = {s.provider: s for s in both_sums}
                self.assertEqual(by_plat["taobao"].status, "auth_required")
                self.assertEqual(by_plat["jd"].status, "success")

    def test_jd_residential_only_filtering_with_purpose_hint(self) -> None:
        def jd_runner(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
            payload = {
                "authRequired": False,
                "currentUrl": "https://pmsearch.jd.com/",
                "extractor": "react-fiber",
                "totalPages": 1,
                "hasNext": False,
                "items": [
                    {
                        "id": "jd_res_ok",
                        "title": "福州市台江区茶亭街道八一七中路与工业路北侧交叉处君临盛世茶亭地块九2#楼3205单元",
                        "url": "https://paimai.jd.com/jd_res_ok",
                        "status": "即将开拍",
                        "initialPrice": 1200000,
                        "housePurpose": "住宅",
                        "location": "福建 福州市 台江区",
                    },
                    {
                        "id": "jd_comm_skip",
                        "title": "福州市仓山区竹榄路清逸华庭11#楼1层11商业服务网点（兼容餐饮）",
                        "url": "https://paimai.jd.com/jd_comm_skip",
                        "status": "正在进行",
                        "initialPrice": 800000,
                        "housePurpose": "",
                        "location": "福建 福州市 仓山区",
                    },
                    {
                        "id": "jd_park_skip",
                        "title": "晋安区鲁能花园一期地下室地下1层0164车位",
                        "url": "https://paimai.jd.com/jd_park_skip",
                        "status": "正在进行",
                        "initialPrice": 90000,
                        "housePurpose": "",
                        "location": "福建 福州市 晋安区",
                    },
                    {
                        "id": "jd_purpose_skip",
                        "title": "福州市鼓楼区五四路168号某房产",
                        "url": "https://paimai.jd.com/jd_purpose_skip",
                        "status": "正在进行",
                        "initialPrice": 2000000,
                        "housePurpose": "商业办公",
                        "location": "福建 福州市 鼓楼区",
                    },
                ],
            }
            return 0, "", f"{MARKER_START}{json.dumps(payload, ensure_ascii=False)}{MARKER_END}"

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = AppConfig(
                storage=StorageConfig(db_path=Path(tmpdir) / "jd_filter.db"),
                provider=ProviderConfig(
                    min_delay_seconds=0,
                    max_delay_seconds=0,
                    max_pages_per_query=1,
                    allowed_property_types=["residential"],
                ),
                regions=[
                    RegionConfig(
                        name="福州市-住宅-京东",
                        provider="jd_ego",
                        province="福建",
                        city="福州",
                        jd_province_id="16",
                        jd_city_id="1303",
                        status_scopes=["ongoing"],
                    )
                ],
            )
            with AuctionDB(cfg.storage.db_path) as db:
                pipeline = SyncPipeline(
                    cfg,
                    db,
                    providers={"jd_ego": JdEgoProvider(cfg.provider, runner=jd_runner)},
                    sleep_fn=lambda _: None,
                )
                sums = pipeline.run_full_cycle(enrich_details=False, refresh_ended=False)
                self.assertEqual(len(sums), 1)
                self.assertEqual(sums[0].items_seen, 4)
                self.assertEqual(sums[0].items_filtered, 3)
                self.assertEqual(sums[0].rounds_upserted, 1)
                rows = db.conn.execute("SELECT item_id, property_type FROM v_auction_full").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["item_id"], "jd_res_ok")
                self.assertEqual(rows[0]["property_type"], "residential")

    def test_upcoming_detail_ignores_lower_recommended_current_price(self) -> None:
        from fph.models import RawAuctionDetail, RawAuctionItem

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = AppConfig(storage=StorageConfig(db_path=Path(tmpdir) / "jd_price.db"))
            with AuctionDB(cfg.storage.db_path) as db:
                db.upsert_list_item(
                    RawAuctionItem(
                        platform="jd",
                        item_id="311515686",
                        url="https://paimai.jd.com/311515686",
                        title="龙岩新罗区某房产",
                        province="福建",
                        city="龙岩",
                        stage="一拍",
                        status="upcoming",
                        initial_price=730000.0,
                        current_price=730000.0,
                        consult_price=912500.0,
                        bid_count=0,
                    )
                )
                # Even if a detail scrape picks up a lower recommended item's price, DB protects upcoming round
                db.apply_detail(
                    RawAuctionDetail(
                        platform="jd",
                        item_id="311515686",
                        url="https://paimai.jd.com/311515686",
                        title="龙岩新罗区某房产",
                        status="upcoming",
                        initial_price=730000.0,
                        current_price=496000.0,
                        bid_count=0,
                    )
                )
                row = db.conn.execute(
                    "SELECT initial_price, current_price FROM auction_rounds WHERE item_id = '311515686'"
                ).fetchone()
                self.assertEqual(row["initial_price"], 730000.0)
                self.assertEqual(row["current_price"], 730000.0)

            def fake_detail_runner(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
                return (
                    0,
                    "__FPH_JSON_START__"
                    + json.dumps(
                        {
                            "authRequired": False,
                            "currentUrl": "https://paimai.jd.com/311515686",
                            "title": "龙岩新罗区某房产",
                            "status": "即将开始",
                            "initialPrice": "73万",
                            "currentPrice": "49.60万",
                            "bidCount": "0",
                        },
                        ensure_ascii=False,
                    )
                    + "__FPH_JSON_END__",
                    "",
                )

            prov = JdEgoProvider(cfg.provider, runner=fake_detail_runner)
            detail = prov.fetch_detail("311515686", "https://paimai.jd.com/311515686")
            self.assertEqual(detail.status, "upcoming")
            self.assertEqual(detail.initial_price, 730000.0)
            self.assertEqual(detail.current_price, 730000.0)

    def test_fetch_detail_rejects_mismatched_item_url_and_script_guards(self) -> None:
        captured: list[str] = []

        def mismatch_runner(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
            captured.append(stdin_text)
            payload = {
                "authRequired": False,
                "currentUrl": "https://paimai.jd.com/311566005",
                "title": "福建省闽侯县甘蔗街道昙石山西大道109号世纪芳洲住宅小区6#楼701单元房产",
                "status": "二拍",
            }
            return 0, f"{MARKER_START}{json.dumps(payload, ensure_ascii=False)}{MARKER_END}", ""

        prov = JdEgoProvider(ProviderConfig(task_space="jd-space"), runner=mismatch_runner)
        with self.assertRaises(ProviderError):
            prov.fetch_detail("311515686", "https://paimai.jd.com/311515686")

        self.assertIn("pmEnsureNotice", captured[0])
        self.assertIn("处置机构", captured[0])
        self.assertIn("标的物所在地", captured[0])
        self.assertIn("标的物详情描述", captured[0])


if __name__ == "__main__":
    unittest.main()
