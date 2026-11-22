from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fph.config import AppConfig, ProviderConfig, RegionConfig, StorageConfig
from fph.db import AuctionDB
from fph.models import RawAuctionItem
from fph.pipeline import SyncPipeline
from fph.providers.base import AuthRequiredError
from fph.providers.taobao_ego import (
    MARKER_END,
    MARKER_START,
    TaobaoEgoProvider,
    _build_detail_script,
    _build_list_script,
)
from fph.providers.taobao_urls import STATUS_SCOPE_TO_ST_PARAMS, build_list_url, expand_status_queries


def _run_generated_script_in_node(script: str, *, dom_env_js: str) -> dict:
    """Execute a generated ego-browser JS script against a lightweight mock DOM in Node.js."""
    harness = f"""
const __domEnv = ({dom_env_js});
let __handedOff = false;
globalThis.location = {{ href: __domEnv.href || 'https://sf.taobao.com/' }};
globalThis.window = {{ scrollBy: () => {{}} }};
globalThis.document = {{
  title: __domEnv.title || '',
  body: {{ innerText: __domEnv.bodyText || '' }},
  querySelector: (sel) => {{
    if (sel === '#sf-item-list-data') return __domEnv.jsonEl ?? null;
    if (sel.includes('#baxia-dialog-content')) return __domEnv.captchaEl ?? null;
    if (sel.includes('page-total')) return __domEnv.pageTotalEl ?? null;
    if (sel.includes('a.next')) return __domEnv.nextBtn ?? null;
    if (sel.includes('h1')) return __domEnv.h1El ?? null;
    if (sel.includes('#J_NoticeDetail')) return __domEnv.noticeEl ?? null;
    if (sel.includes('#J_desc')) return __domEnv.surveyEl ?? null;
    if (sel.includes('.unit-txt')) return __domEnv.courtEl ?? null;
    return null;
  }},
  querySelectorAll: (sel) => {{
    if (sel.includes('sf_item/')) return __domEnv.itemLinks || [];
    return [];
  }},
}};
globalThis.listTaskSpaces = async () => [{{ id: 's1', name: 'test-space', ownership: 'agent' }}];
globalThis.taskSpace = async () => ({{
  pages: async () => [{{
    goto: async () => {{}},
    waitForTimeout: async () => {{}},
    evaluate: async (fn) => fn(),
  }}],
  handOff: async () => {{ __handedOff = true; }},
}});
"""
    proc = subprocess.run(
        ["node", "-e", harness + "\n" + script],
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node failed ({proc.returncode}): {proc.stderr or proc.stdout}")
    start = proc.stdout.index(MARKER_START) + len(MARKER_START)
    end = proc.stdout.index(MARKER_END, start)
    return json.loads(proc.stdout[start:end])


class TaobaoEgoAndPipelineTest(unittest.TestCase):
    def test_status_scope_mapping(self) -> None:
        self.assertEqual(STATUS_SCOPE_TO_ST_PARAMS["active"], [("ongoing", "0"), ("upcoming", "1")])
        self.assertEqual(STATUS_SCOPE_TO_ST_PARAMS["ongoing"], [("ongoing", "0")])
        self.assertEqual(STATUS_SCOPE_TO_ST_PARAMS["upcoming"], [("upcoming", "1")])
        self.assertEqual(STATUS_SCOPE_TO_ST_PARAMS["ended"], [("ended", "2")])
        self.assertEqual(STATUS_SCOPE_TO_ST_PARAMS["all"], [("all", "-1")])
        self.assertEqual(expand_status_queries("active"), [("ongoing", "0"), ("upcoming", "1")])
        self.assertEqual(expand_status_queries("ended"), [("ended", "2")])

    def test_url_builder_gbk_city_province_and_location_code_fallback(self) -> None:
        # 1. City path takes precedence over legacy location_code (strips trailing '市')
        fz = RegionConfig(name="福州市-住宅", province="福建省", city="福州市", location_code="350100")
        url_fz_ongoing = build_list_url(fz, st_param="0", page=1)
        self.assertIn("https://sf.taobao.com/list/50025969__0___%B8%A3%D6%DD.htm?", url_fz_ongoing)
        self.assertIn("auction_source=0", url_fz_ongoing)
        self.assertIn("st_param=4", url_fz_ongoing)
        self.assertIn("auction_start_seg=-1", url_fz_ongoing)
        self.assertNotIn("location_code=", url_fz_ongoing)
        self.assertNotIn("page=", url_fz_ongoing)

        url_fz_page2 = build_list_url(fz, st_param="1", page=2, lookback_days=730, reference_date=date(2026, 11, 21))
        self.assertIn("/list/50025969__1___%B8%A3%D6%DD.htm?", url_fz_page2)
        self.assertIn("st_param=4", url_fz_page2)
        self.assertIn("auction_start_seg=-1", url_fz_page2)
        self.assertNotIn("auction_start_from=", url_fz_page2)
        self.assertIn("page=2", url_fz_page2)

        # 2. Ended (__2) with lookback_days adds date range filter
        url_fz_ended = build_list_url(
            fz,
            st_param="2",
            page=1,
            lookback_days=730,
            reference_date=date(2026, 11, 21),
        )
        self.assertIn("/list/50025969__2___%B8%A3%D6%DD.htm?", url_fz_ended)
        self.assertIn("st_param=4", url_fz_ended)
        self.assertIn("auction_start_seg=0", url_fz_ended)
        self.assertIn("auction_start_from=2024-11-21", url_fz_ended)
        self.assertIn("auction_start_to=2026-11-21", url_fz_ended)

        # 3. Province-only fallback (2 underscores, strips trailing '省')
        fj_only = RegionConfig(name="福建省-住宅", province="福建省", city="", location_code="350000")
        url_fj = build_list_url(fj_only, st_param="0", page=1)
        self.assertIn("/list/50025969__0__%B8%A3%BD%A8.htm?", url_fj)
        self.assertNotIn("___", url_fj)
        self.assertNotIn("location_code=", url_fj)
        self.assertIn("st_param=4", url_fj)

        # 4. location_code fallback only when both city and province are empty
        code_only = RegionConfig(name="代码回退", province="", city="", location_code="350100")
        url_code = build_list_url(code_only, st_param="0", page=3)
        self.assertIn("/list/50025969__0.htm?", url_code)
        self.assertIn("location_code=350100", url_code)
        self.assertIn("st_param=4", url_code)
        self.assertIn("page=3", url_code)

        # 5. No region fallback
        no_region = RegionConfig(name="全国", province="", city="", location_code="")
        url_all = build_list_url(no_region, st_param="-1", page=1)
        self.assertIn("/list/50025969__-1.htm?", url_all)
        self.assertNotIn("location_code=", url_all)
        self.assertIn("st_param=4", url_all)

        # 6. custom_url preserves path & custom sort param, appends lookback only when needed
        custom_region = RegionConfig(
            name="自定义",
            province="福建",
            city="福州",
            custom_url="https://sf.taobao.com/list/50025969__2___%B8%A3%D6%DD.htm?auction_source=0&st_param=2&auction_start_seg=-1&page=9",
        )
        url_custom = build_list_url(
            custom_region,
            st_param="2",
            page=2,
            lookback_days=30,
            reference_date=date(2026, 11, 21),
        )
        self.assertIn("/list/50025969__2___%B8%A3%D6%DD.htm?", url_custom)
        self.assertIn("st_param=2", url_custom)
        self.assertIn("auction_start_seg=0", url_custom)
        self.assertIn("auction_start_from=2026-10-22", url_custom)
        self.assertIn("auction_start_to=2026-11-21", url_custom)
        self.assertIn("page=2", url_custom)
        self.assertNotIn("page=9", url_custom)

        custom_explicit_dates = RegionConfig(
            name="自定义日期",
            province="福建",
            city="福州",
            custom_url="https://sf.taobao.com/list/50025969__2___%B8%A3%D6%DD.htm?st_param=1&auction_start_seg=0&auction_start_from=2025-01-01&auction_start_to=2025-12-31",
        )
        url_explicit = build_list_url(
            custom_explicit_dates,
            st_param="2",
            page=1,
            lookback_days=30,
            reference_date=date(2026, 11, 21),
        )
        self.assertIn("st_param=1", url_explicit)
        self.assertIn("auction_start_from=2025-01-01", url_explicit)
        self.assertIn("auction_start_to=2025-12-31", url_explicit)

    def test_preview_list_url_passes_config_lookback_days(self) -> None:
        cfg = ProviderConfig()
        setattr(cfg, "lookback_days", 180)
        provider = TaobaoEgoProvider(cfg)
        region = RegionConfig(name="福州市-住宅", province="福建", city="福州", location_code="350100")
        ended_url = provider.preview_list_url(region, st_param="2", page=1)
        self.assertIn("/list/50025969__2___%B8%A3%D6%DD.htm?", ended_url)
        self.assertIn("auction_start_seg=0", ended_url)
        self.assertIn("auction_start_from=", ended_url)
        self.assertIn("auction_start_to=", ended_url)

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
            provider.fetch_list_page(region, status_scope="0", page=1)
        self.assertEqual(ctx.exception.task_space, "test-space")

    def test_slider_captcha_text_triggers_auth_required_in_detail_and_list(self) -> None:
        # Python-side guard: long slider noticeText (>1500 chars) must still raise AuthRequiredError
        long_captcha = ("安全提示 " * 400) + "请拖动下方滑块完成验证，通过验证以确保正常访问"

        def detail_captcha_runner(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
            payload = {
                "authRequired": False,
                "currentUrl": "https://sf-item.taobao.com/sf_item/999001.htm",
                "title": "验证页面",
                "noticeText": long_captcha,
                "surveyText": "",
            }
            return 0, f"{MARKER_START}{json.dumps(payload, ensure_ascii=False)}{MARKER_END}", ""

        provider = TaobaoEgoProvider(ProviderConfig(task_space="test-space"), runner=detail_captcha_runner)
        with self.assertRaises(AuthRequiredError) as ctx:
            provider.fetch_detail("999001", "https://sf-item.taobao.com/sf_item/999001.htm")
        self.assertEqual(ctx.exception.task_space, "test-space")

        def list_captcha_runner(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
            payload = {
                "authRequired": False,
                "currentUrl": "https://sf.taobao.com/list/50025969__0___%B8%A3%D6%DD.htm?x5sec=abc",
                "pageTitle": "阿里拍卖",
                "items": [],
            }
            return 0, f"{MARKER_START}{json.dumps(payload, ensure_ascii=False)}{MARKER_END}", ""

        provider_list = TaobaoEgoProvider(ProviderConfig(task_space="test-space"), runner=list_captcha_runner)
        region = RegionConfig(name="福州市-住宅", province="福建", city="福州", location_code="350100")
        with self.assertRaises(AuthRequiredError):
            provider_list.fetch_list_page(region, status_scope="0", page=1)

    def test_generated_js_slider_detection_and_no_recommended_fallback(self) -> None:
        list_script = _build_list_script("test-space", "https://sf.taobao.com/list/50025969__0___%B8%A3%D6%DD.htm", 20)
        detail_script = _build_detail_script("test-space", "https://sf-item.taobao.com/sf_item/12345.htm", 20)

        for keyword in ("拖动下方滑块", "请按住滑块", "滑动验证", "滑块验证", "完成验证", "通过验证以确保正常访问", "baxia", "punish", "x5sec"):
            self.assertIn(keyword, list_script)
            self.assertIn(keyword, detail_script)
        self.assertIn("没有您要找的标的物", list_script)

        if not shutil.which("node"):
            return

        # 1. Detail page with >2000 chars body containing slider text must set authRequired=true
        long_body = ("导航栏内容 法律声明 " * 300) + "请按住滑块，拖动到最右边，通过验证以确保正常访问"
        detail_res = _run_generated_script_in_node(
            detail_script,
            dom_env_js=json.dumps(
                {
                    "href": "https://sf-item.taobao.com/sf_item/12345.htm",
                    "title": "福州市鼓楼区某住宅",
                    "bodyText": long_body,
                },
                ensure_ascii=False,
            ),
        )
        self.assertTrue(detail_res["authRequired"])

        # 2. List page with slider text must set authRequired=true even when #sf-item-list-data is present
        list_captcha_res = _run_generated_script_in_node(
            list_script,
            dom_env_js=json.dumps(
                {
                    "href": "https://sf.taobao.com/list/50025969__0___%B8%A3%D6%DD.htm",
                    "title": "淘宝司法拍卖",
                    "bodyText": "拖动下方滑块完成验证",
                    "jsonEl": {"textContent": '{"data": []}'},
                },
                ensure_ascii=False,
            ),
        )
        self.assertTrue(list_captcha_res["authRequired"])

        # 3. List page with empty #sf-item-list-data + recommended DOM cards must return items=[]
        empty_json_dom = """{
          href: 'https://sf.taobao.com/list/50025969__0___%B8%A3%D6%DD.htm',
          title: '福州住宅拍卖',
          bodyText: '已为您推荐更多相关拍品',
          jsonEl: { textContent: '{"data": []}' },
          itemLinks: [{
            href: '//sf-item.taobao.com/sf_item/777777.htm',
            getAttribute: (k) => k === 'href' ? '//sf-item.taobao.com/sf_item/777777.htm' : '其他城市推荐拍品',
            innerText: '其他城市推荐拍品',
            parentElement: { innerText: '其他城市推荐拍品 起拍价 ¥50万' }
          }]
        }"""
        empty_json_res = _run_generated_script_in_node(list_script, dom_env_js=empty_json_dom)
        self.assertFalse(empty_json_res["authRequired"])
        self.assertEqual(empty_json_res["items"], [])
        self.assertFalse(empty_json_res["hasNext"])

        # 4. List page without #sf-item-list-data but with '没有您要找的标的物' must return items=[]
        no_result_dom = """{
          href: 'https://sf.taobao.com/list/50025969__0___%B8%A3%D6%DD.htm',
          title: '福州住宅拍卖',
          bodyText: '亲，请登录 很抱歉，没有您要找的标的物。已为您推荐更多相关拍品',
          jsonEl: null,
          itemLinks: [{
            href: '//sf-item.taobao.com/sf_item/888888.htm',
            getAttribute: (k) => k === 'href' ? '//sf-item.taobao.com/sf_item/888888.htm' : '推荐房源',
            innerText: '推荐房源',
            parentElement: { innerText: '推荐房源 起拍价 ¥88万' }
          }]
        }"""
        no_result_res = _run_generated_script_in_node(list_script, dom_env_js=no_result_dom)
        self.assertFalse(no_result_res["authRequired"])
        self.assertEqual(no_result_res["items"], [])
        self.assertFalse(no_result_res["hasNext"])

    def test_html_entity_unescape_and_status_fallback(self) -> None:
        def fake_runner(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
            if "sf_item/555001" in stdin_text:
                payload = {
                    "authRequired": False,
                    "currentUrl": "https://sf-item.taobao.com/sf_item/555001.htm",
                    "title": "阳光城&middot;丽景湾&bull;1幢501室",
                    "status": "revocation",
                    "courtName": "福州市鼓楼区&amp;人民法院",
                    "address": "福州市仓山区阳光城&middot;丽景湾1#501",
                    "noticeText": "建筑面积100平方米。",
                    "surveyText": "",
                }
                return 0, f"{MARKER_START}{json.dumps(payload, ensure_ascii=False)}{MARKER_END}", ""

            payload = {
                "authRequired": False,
                "currentUrl": "https://sf.taobao.com/list/50025969__0___%B8%A3%D6%DD.htm?auction_source=0&st_param=4&auction_start_seg=-1",
                "extractor": "sf-item-list-data",
                "totalPages": 1,
                "hasNext": False,
                "items": [
                    {
                        "id": "555001",
                        "title": "阳光城&middot;丽景湾&bull;1幢501室",
                        "url": "//sf-item.taobao.com/sf_item/555001.htm",
                        "status": "",
                        "courtName": "福州市鼓楼区&amp;人民法院",
                        "address": "福州市仓山区阳光城&middot;丽景湾",
                    },
                    {
                        "id": "555002",
                        "title": "【一拍】仓山区某小区2幢302室",
                        "url": "//sf-item.taobao.com/sf_item/555002.htm",
                        "status": "revocation",
                    },
                ],
            }
            return 0, f"{MARKER_START}{json.dumps(payload, ensure_ascii=False)}{MARKER_END}", ""

        provider = TaobaoEgoProvider(ProviderConfig(task_space="test-space"), runner=fake_runner)
        region = RegionConfig(name="福州市-住宅", province="福建", city="福州", location_code="350100")

        # st_param="0" fallback -> ongoing
        page_ongoing = provider.fetch_list_page(region, status_scope="0", page=1)
        self.assertEqual(page_ongoing.items[0].title, "阳光城·丽景湾•1幢501室")
        self.assertEqual(page_ongoing.items[0].court_name, "福州市鼓楼区&人民法院")
        self.assertEqual(page_ongoing.items[0].address, "福州市仓山区阳光城·丽景湾")
        self.assertEqual(page_ongoing.items[0].status, "ongoing")
        self.assertEqual(page_ongoing.items[1].status, "withdrawn")

        # st_param="1" fallback -> upcoming
        page_upcoming = provider.fetch_list_page(region, status_scope="1", page=1)
        self.assertEqual(page_upcoming.items[0].status, "upcoming")

        # st_param="2" fallback -> ended
        page_ended = provider.fetch_list_page(region, status_scope="2", page=1)
        self.assertEqual(page_ended.items[0].status, "ended")

        detail = provider.fetch_detail("555001", "https://sf-item.taobao.com/sf_item/555001.htm")
        self.assertEqual(detail.title, "阳光城·丽景湾•1幢501室")
        self.assertEqual(detail.court_name, "福州市鼓楼区&人民法院")
        self.assertEqual(detail.address, "福州市仓山区阳光城·丽景湾1#501")
        self.assertEqual(detail.status, "withdrawn")

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
                    "currentUrl": "https://sf.taobao.com/list/50025969__2___%C1%FA%D1%D2.htm?auction_source=0&st_param=4&auction_start_seg=-1",
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
                self.assertIn("/list/50025969__2___%C1%FA%D1%D2.htm", calls[0])
                self.assertIn("st_param=4", calls[0])

                row = db.conn.execute("SELECT * FROM v_auction_full WHERE item_id = '888001'").fetchone()
                self.assertEqual(row["city"], "龙岩")
                self.assertEqual(row["district"], "新罗区")
                self.assertEqual(row["status"], "sold")
                self.assertEqual(row["area_sqm"], 110.5)
                self.assertEqual(row["court_name"], "龙岩市新罗区人民法院")

    def test_allowed_property_types_filters_list_enrich_and_ended_refresh(self) -> None:
        detail_calls: list[str] = []

        def fake_runner(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
            for item_id in ("tb_res_1", "tb_park_1", "tb_comm_1", "tb_misc_1", "old_park_1"):
                if f"sf_item/{item_id}" in stdin_text:
                    detail_calls.append(item_id)
                    payload = {
                        "authRequired": False,
                        "currentUrl": f"https://sf-item.taobao.com/sf_item/{item_id}.htm",
                        "title": "【二拍】福州市鼓楼区温泉花园1幢202室住宅",
                        "status": "拍卖已成交",
                        "currentPrice": "¥160万",
                        "noticeText": "建筑面积96平方米，已腾空。",
                    }
                    return 0, f"{MARKER_START}{json.dumps(payload, ensure_ascii=False)}{MARKER_END}", ""

            payload = {
                "authRequired": False,
                "currentUrl": "https://sf.taobao.com/list/50025969__2___%B8%A3%D6%DD.htm?auction_source=0&st_param=4&auction_start_seg=-1",
                "extractor": "sf-item-list-data",
                "totalPages": 1,
                "hasNext": False,
                "items": [
                    {
                        "id": "tb_res_1",
                        "title": "【二拍】福州市鼓楼区温泉花园1幢202室住宅",
                        "url": "//sf-item.taobao.com/sf_item/tb_res_1.htm",
                        "status": "ended",
                        "stage": "二拍",
                        "initialPrice": 1500000,
                        "currentPrice": 1600000,
                        "consultPrice": 2000000,
                        "bidCount": 2,
                        "end": "2026-10-01 10:00:00",
                    },
                    {
                        "id": "tb_park_1",
                        "title": "福州市鼓楼区温泉花园地下一层101车位",
                        "url": "//sf-item.taobao.com/sf_item/tb_park_1.htm",
                        "status": "ongoing",
                        "initialPrice": 80000,
                    },
                    {
                        "id": "tb_comm_1",
                        "title": "福州市台江区万达广场一层108号商铺",
                        "url": "//sf-item.taobao.com/sf_item/tb_comm_1.htm",
                        "status": "upcoming",
                        "initialPrice": 1800000,
                    },
                    {
                        "id": "tb_misc_1",
                        "title": "钟馗摆件",
                        "url": "//sf-item.taobao.com/sf_item/tb_misc_1.htm",
                        "status": "failed",
                        "initialPrice": 1000,
                    },
                ],
            }
            return 0, f"{MARKER_START}{json.dumps(payload, ensure_ascii=False)}{MARKER_END}", ""

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = AppConfig(
                storage=StorageConfig(db_path=Path(tmpdir) / "fph_filter.db"),
                provider=ProviderConfig(
                    min_delay_seconds=0,
                    max_delay_seconds=0,
                    max_pages_per_query=1,
                    allowed_property_types=["residential"],
                ),
                regions=[
                    RegionConfig(
                        name="福州市-住宅",
                        province="福建",
                        city="福州",
                        location_code="350100",
                        status_scopes=["ended"],
                    )
                ],
            )
            with AuctionDB(cfg.storage.db_path) as db:
                # Pre-existing non-residential round in DB must also be skipped by enrich & ended refresh
                db.upsert_list_item(
                    RawAuctionItem(
                        platform="taobao",
                        item_id="old_park_1",
                        url="https://sf-item.taobao.com/sf_item/old_park_1.htm",
                        title="福州市晋安区某小区地下1层08号车位",
                        province="福建",
                        city="福州",
                        status="ongoing",
                        end_time="2020-01-01 10:00:00",
                        category_hint="50025969",
                    )
                )

                provider = TaobaoEgoProvider(cfg.provider, runner=fake_runner)
                pipeline = SyncPipeline(cfg, db, provider, sleep_fn=lambda _: None)
                summaries = pipeline.run_full_cycle(enrich_details=True, refresh_ended=True)

                self.assertEqual(len(summaries), 1)
                self.assertEqual(summaries[0].items_seen, 4)
                self.assertEqual(summaries[0].items_filtered, 3)
                self.assertEqual(summaries[0].properties_created, 1)
                self.assertEqual(summaries[0].rounds_upserted, 1)
                self.assertEqual(summaries[0].details_enriched, 1)
                self.assertEqual(detail_calls, ["tb_res_1"])

                saved_ids = [
                    r[0]
                    for r in db.conn.execute("SELECT item_id FROM auction_rounds ORDER BY item_id").fetchall()
                ]
                self.assertEqual(saved_ids, ["old_park_1", "tb_res_1"])

                # Region-level allowed_property_types=[] overrides global residential-only filter
                open_region = RegionConfig(
                    name="福州市-全类型",
                    province="福建",
                    city="福州",
                    location_code="350100",
                    status_scopes=["ended"],
                    allowed_property_types=[],
                )
                open_sum = pipeline.sync_region(open_region)
                self.assertEqual(open_sum.items_seen, 4)
                self.assertEqual(open_sum.items_filtered, 0)
                self.assertEqual(
                    open_sum.rounds_upserted + getattr(open_sum, "items_skipped_existing", 0),
                    4,
                )
                all_saved_ids = [
                    r[0]
                    for r in db.conn.execute("SELECT item_id FROM auction_rounds ORDER BY item_id").fetchall()
                ]
                self.assertEqual(
                    all_saved_ids,
                    ["old_park_1", "tb_comm_1", "tb_misc_1", "tb_park_1", "tb_res_1"],
                )

    def test_selloff_flag_maps_to_bianmai_stage_and_detail_updates_stage(self) -> None:
        def fake_runner(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
            if "sf_item/1002" in stdin_text:
                payload = {
                    "authRequired": False,
                    "currentUrl": "https://sf-item.taobao.com/sf_item/1002.htm",
                    "title": "福州市晋安区某花园2幢202室",
                    "stage": "二拍",
                    "status": "正在进行",
                    "noticeText": "第二次拍卖，建筑面积95平方米。",
                }
            else:
                payload = {
                    "authRequired": False,
                    "currentUrl": "https://sf.taobao.com/list/50025969__0___%B8%A3%D6%DD.htm",
                    "extractor": "sf-item-list-data",
                    "hasNext": False,
                    "items": [
                        {
                            "id": "1001",
                            "title": "福州市仓山区某小区1幢101室",
                            "url": "//sf-item.taobao.com/sf_item/1001.htm",
                            "status": "doing",
                            "raw": {"id": 1001, "sellOff": True},
                        },
                        {
                            "id": "1002",
                            "title": "福州市晋安区某花园2幢202室",
                            "url": "//sf-item.taobao.com/sf_item/1002.htm",
                            "status": "doing",
                            "raw": {"id": 1002, "sellOff": False},
                        },
                    ],
                }
            return 0, f"{MARKER_START}{json.dumps(payload, ensure_ascii=False)}{MARKER_END}", ""

        prov = TaobaoEgoProvider(ProviderConfig(), runner=fake_runner)
        reg = RegionConfig(name="福州", province="福建", city="福州")
        items = prov.fetch_list_page(reg, "0", 1).items
        self.assertEqual(items[0].stage, "变卖")
        self.assertEqual(items[1].stage, "未知")

        with tempfile.TemporaryDirectory() as tmpdir:
            with AuctionDB(Path(tmpdir) / "stage.db") as db:
                db.upsert_list_item(items[1])
                detail = prov.fetch_detail("1002", items[1].url)
                db.apply_detail(detail)
                self.assertEqual(
                    db.conn.execute("SELECT stage FROM auction_rounds WHERE item_id = '1002'").fetchone()[0],
                    "二拍",
                )


if __name__ == "__main__":
    unittest.main()
