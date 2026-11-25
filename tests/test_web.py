from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from fph.config import AppConfig, RegionConfig, StorageConfig
from fph.db import AuctionDB
from fph.models import RawAuctionDetail, RawAuctionItem, SyncSummary
from fph.providers.base import AuctionProvider, AuthRequiredError
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

    def _post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        req = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_html_and_overview(self) -> None:
        with urlopen(f"{self.base_url}/", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            html = resp.read().decode("utf-8")
            self.assertIn("FPH 法拍房数据看板", html)
            self.assertIn("stageMulti", html)
            self.assertIn("statusMulti", html)
            self.assertIn("bStageMulti", html)
            self.assertIn("districtMulti", html)
            self.assertIn("bDistrictMulti", html)
            self.assertIn("toastStack", html)
            self.assertIn("confirmBackdrop", html)
            self.assertIn("drawerEnrichBar", html)
            self.assertIn("globalTaskPill", html)
            self.assertIn("filterDetailState", html)
            self.assertIn("btnEnrichMissing", html)
            self.assertNotIn("filterExcludeSelloff", html)
            self.assertIn("tab-console", html)
            self.assertIn("btnQuickSync7d", html)
            self.assertIn("consoleLogBox", html)

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

        other_status = self._get_json("/api/items?status=other")
        self.assertEqual(other_status["total"], 1)
        self.assertEqual(other_status["items"][0]["item_id"], "w_404_jd")

        multi_status = self._get_json("/api/items?status=ongoing,upcoming")
        self.assertEqual(multi_status["total"], 1)

        repeated_status = self._get_json("/api/items?status=upcoming&status=other")
        self.assertEqual(repeated_status["total"], 2)

        multi_stage = self._get_json(f"/api/items?stage={quote('一拍,二拍')}")
        self.assertEqual(multi_stage["total"], 5)

        only_second = self._get_json(f"/api/items?stage={quote('二拍')}&exclude_stage={quote('一拍')}")
        self.assertEqual(only_second["total"], 2)

        no_first = self._get_json(f"/api/items?exclude_stage={quote('一拍')}")
        self.assertEqual(no_first["total"], 2)

        multi_district = self._get_json(f"/api/items?district={quote('鼓楼区,仓山区')}")
        self.assertEqual(multi_district["total"], 3)

        repeated_district = self._get_json(f"/api/items?district={quote('仓山区')}&district={quote('新罗区')}")
        self.assertEqual(repeated_district["total"], 2)

        missing_metric = self._get_json("/api/items?detail_state=missing_metric")
        self.assertEqual(missing_metric["total"], 2)
        self.assertTrue(
            all(it["area_sqm"] is None or it["unit_price"] is None for it in missing_metric["items"])
        )

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

        bargains_multi_d = self._get_json(f"/api/bargains?max_discount=0.75&district={quote('鼓楼区,仓山区')}")
        self.assertEqual(len(bargains_multi_d["rows"]), 1)
        self.assertEqual(bargains_multi_d["rows"][0]["district"], "仓山区")

        bargains_other_d = self._get_json(f"/api/bargains?max_discount=0.75&district={quote('鼓楼区')}")
        self.assertEqual(len(bargains_other_d["rows"]), 0)

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

        with urlopen(f"{self.base_url}/api/export?dataset=bargains&format=csv&max_discount=0.75&district={quote('鼓楼区')}", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            b_empty = resp.read().decode("utf-8-sig")
            self.assertNotIn("w_202_r2", b_empty)

        with urlopen(f"{self.base_url}/api/export?dataset=bargains&format=csv&max_discount=0.75&district={quote('仓山区')}", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            b_match = resp.read().decode("utf-8-sig")
            self.assertIn("w_202_r2", b_match)

    def test_batch_enrich_skip_limit_auth_pause_resume_and_cancel(self) -> None:
        class _BatchStubProvider(AuctionProvider):
            platform = "taobao"

            def __init__(self) -> None:
                self.calls: list[str] = []
                self.fail_auth_once = True

            def check_ready(self) -> tuple[bool, str]:
                return True, "ok"

            def fetch_list_page(self, region, status_scope, page):  # pragma: no cover
                raise NotImplementedError

            def fetch_detail(self, item_id: str, url: str) -> RawAuctionDetail:
                self.calls.append(item_id)
                if item_id == "w_202_r2" and self.fail_auth_once:
                    self.fail_auth_once = False
                    raise AuthRequiredError("触发滑块验证", url=url, task_space="test-space")
                return RawAuctionDetail(
                    platform="taobao",
                    item_id=item_id,
                    url=url,
                    notice_text="标的物建筑面积99.5平方米，已腾空。",
                )

        stub = _BatchStubProvider()
        server = create_web_server(
            AppConfig(storage=StorageConfig(db_path=self.db_path)),
            host="127.0.0.1",
            port=0,
            provider_factory=lambda _cfg: {"taobao_ego": stub},
            sleep_fn=lambda _: None,
        )
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def _req(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
            req = Request(
                f"{base}{path}",
                data=(json.dumps(body).encode("utf-8") if body is not None else None),
                headers={"Content-Type": "application/json"} if body is not None else {},
                method=method,
            )
            try:
                with urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

        try:
            # 1. Validation: empty or > 30 rejected
            c_empty, _ = _req("POST", "/api/enrich", {"round_ids": []})
            self.assertEqual(c_empty, 400)
            c_over, _ = _req("POST", "/api/enrich", {"round_ids": list(range(1, 32))})
            self.assertEqual(c_over, 400)

            # Find round_id of w_202_r2 (unenriched) and self.r2_id (already enriched)
            with AuctionDB(self.db_path) as db:
                r_un = db.conn.execute(
                    "SELECT id FROM auction_rounds WHERE item_id = 'w_202_r2'"
                ).fetchone()[0]

            # 2. Start batch with [r2_id (enriched), r_un (unenriched)] and force=False:
            # r2_id is skipped, r_un hits AuthRequiredError and pauses
            code, snap = _req("POST", "/api/enrich", {"round_ids": [self.r2_id, r_un], "force": False})
            self.assertEqual(code, 202)
            self.assertEqual(snap["skipped"], 1)

            for _ in range(40):
                _, st = _req("GET", "/api/enrich/status")
                if st["status"] == "auth_required":
                    break
                time.sleep(0.05)
            self.assertEqual(st["status"], "auth_required")
            self.assertEqual(st["current_item_id"], "w_202_r2")

            # Starting another job while paused returns 409 conflict
            c_conf, _ = _req("POST", "/api/enrich", {"round_ids": [self.r2_id], "force": True})
            self.assertEqual(c_conf, 409)

            # 3. Resume -> retries w_202_r2 and completes
            c_res, _ = _req("POST", "/api/enrich/resume", {})
            self.assertEqual(c_res, 200)
            for _ in range(40):
                _, st = _req("GET", "/api/enrich/status")
                if st["status"] == "completed":
                    break
                time.sleep(0.05)
            self.assertEqual(st["status"], "completed")
            self.assertEqual(st["succeeded"], 1)
            self.assertEqual(st["skipped"], 1)
            self.assertEqual(stub.calls, ["w_202_r2", "w_202_r2"])

            # 4. Force re-enrich already-enriched item works
            c_force, snap_f = _req("POST", "/api/enrich", {"round_ids": [self.r2_id], "force": True})
            self.assertEqual(c_force, 202)
            for _ in range(40):
                _, st = _req("GET", "/api/enrich/status")
                if st["status"] == "completed":
                    break
                time.sleep(0.05)
            self.assertEqual(st["succeeded"], 1)
            self.assertEqual(st["skipped"], 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_console_options_stream_logs_auth_retry_stop_and_mutex(self) -> None:
        class _FakeProc:
            def __init__(self, lines: list[str], exit_code: int, *, block_event: threading.Event | None = None) -> None:
                self._lines = lines
                self._exit_code = exit_code
                self._block_event = block_event
                self._terminated = False
                self.stdout = self._iter_lines()

            def _iter_lines(self) -> io.StringIO:
                # Build an iterator-compatible wrapper using a pipe-like generator via StringIO when non-blocking,
                # or a custom object with readline().
                return io.StringIO("".join(f"{ln}\n" for ln in self._lines))

            def wait(self, timeout: int | None = None) -> int:
                if self._block_event is not None:
                    self._block_event.wait(timeout=timeout or 5)
                return -15 if self._terminated else self._exit_code

            def terminate(self) -> None:
                self._terminated = True
                if self._block_event is not None:
                    self._block_event.set()

            def kill(self) -> None:
                self.terminate()

        class _BlockingProc:
            def __init__(self) -> None:
                self._ev = threading.Event()
                self._terminated = False
                self.stdout = self

            def readline(self) -> str:
                self._ev.wait(timeout=5)
                return ""

            def wait(self, timeout: int | None = None) -> int:
                self._ev.wait(timeout=timeout or 5)
                return -15 if self._terminated else 0

            def terminate(self) -> None:
                self._terminated = True
                self._ev.set()

            def kill(self) -> None:
                self.terminate()

        captured_cmds: list[list[str]] = []
        mode = {"kind": "auth"}

        def fake_process_factory(cmd: list[str], env: dict[str, str]):
            captured_cmds.append(cmd)
            self.assertEqual(env.get("PYTHONUNBUFFERED"), "1")
            if mode["kind"] == "auth":
                return _FakeProc(["[INFO] 采集开始", "[WARNING] 检测到滑块验证 auth_required"], 2)
            if mode["kind"] == "ok":
                return _FakeProc(["[INFO] 从断点续采完成", "[INFO] 新增 3 条"], 0)
            return _BlockingProc()

        cfg = AppConfig(
            storage=StorageConfig(db_path=self.db_path),
            regions=[RegionConfig(name="福州市-住宅", province="福建", city="福州", location_code="350100")],
        )
        server = create_web_server(
            cfg,
            host="127.0.0.1",
            port=0,
            process_factory=fake_process_factory,
        )
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def _req(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
            req = Request(
                f"{base}{path}",
                data=(json.dumps(body).encode("utf-8") if body is not None else None),
                headers={"Content-Type": "application/json"} if body is not None else {},
                method=method,
            )
            try:
                with urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

        try:
            # 1. Options endpoint
            c_opt, opts = _req("GET", "/api/console/options")
            self.assertEqual(c_opt, 200)
            self.assertEqual(opts["regions"][0]["name"], "福州市-住宅")

            # 2. Invalid action rejected (400)
            c_bad, _ = _req("POST", "/api/console/run", {"action": "rm_rf"})
            self.assertEqual(c_bad, 400)

            # 3. Run sync with lookback_days=7 & reset_checkpoint=True -> hits auth_required
            c_run, snap = _req(
                "POST",
                "/api/console/run",
                {"action": "sync", "params": {"lookback_days": 7, "reset_checkpoint": True, "provider": "taobao"}},
            )
            self.assertEqual(c_run, 202)
            self.assertIn("--lookback-days", captured_cmds[0])
            self.assertIn("7", captured_cmds[0])
            self.assertIn("--no-wait-auth", captured_cmds[0])

            for _ in range(40):
                _, st = _req("GET", "/api/console/status?since=0")
                if st["status"] == "auth_required":
                    break
                time.sleep(0.05)
            self.assertEqual(st["status"], "auth_required")
            self.assertTrue(st["needs_auth"])
            self.assertTrue(any("滑块验证" in ln["text"] for ln in st["lines"]))

            # 4. Retry drops --reset-checkpoint and succeeds
            mode["kind"] = "ok"
            c_ret, _ = _req("POST", "/api/console/retry", {})
            self.assertEqual(c_ret, 202)
            self.assertNotIn("--reset-checkpoint", captured_cmds[1])
            for _ in range(40):
                _, st2 = _req("GET", "/api/console/status?since=0")
                if st2["status"] == "succeeded":
                    break
                time.sleep(0.05)
            self.assertEqual(st2["status"], "succeeded")

            # 5. Running blocking sync blocks /api/enrich with 409 Conflict, and /api/console/stop terminates it
            mode["kind"] = "block"
            c_blk, _ = _req("POST", "/api/console/run", {"action": "sync", "params": {"active_only": True}})
            self.assertEqual(c_blk, 202)
            c_conf, _ = _req("POST", "/api/enrich", {"round_ids": [self.r2_id], "force": True})
            self.assertEqual(c_conf, 409)

            c_stop, _ = _req("POST", "/api/console/stop", {})
            self.assertEqual(c_stop, 200)
            for _ in range(40):
                _, st3 = _req("GET", "/api/console/status?since=0")
                if st3["status"] == "stopped":
                    break
                time.sleep(0.05)
            self.assertEqual(st3["status"], "stopped")
        finally:
            server.cli_manager.stop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_post_csrf_guards_and_enrich_unexpected_exception_recovery(self) -> None:
        class _ExplodingProvider:
            platform = "taobao"

            def fetch_detail(self, item_id: str, url: str):
                raise RuntimeError("unexpected boom")

        cfg = AppConfig(
            storage=StorageConfig(db_path=self.db_path),
            regions=[RegionConfig(name="福州市-住宅", province="福建", city="福州", location_code="350100")],
        )
        server = create_web_server(
            cfg,
            host="127.0.0.1",
            port=0,
            provider_factory=lambda _c: {"taobao": _ExplodingProvider()},
            sleep_fn=lambda _s: None,
        )
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # 1. Cross-site Origin and non-JSON Content-Type rejected with 403
            for hdrs in [
                {"Content-Type": "text/plain"},
                {"Content-Type": "application/json", "Origin": "https://evil.example"},
            ]:
                req = Request(f"{base}/api/console/stop", data=b"{}", headers=hdrs, method="POST")
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 403)
                ctx.exception.close()

            # 2. Unexpected exception in EnrichJobManager does not leave status stuck in 'running'
            req_ok = Request(
                f"{base}/api/enrich",
                data=json.dumps({"round_ids": [self.r2_id], "force": True}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Origin": base},
                method="POST",
            )
            with urlopen(req_ok, timeout=5) as resp:
                self.assertEqual(resp.status, 202)
            final_snap = None
            for _ in range(40):
                with urlopen(f"{base}/api/enrich/status", timeout=5) as resp:
                    final_snap = json.loads(resp.read().decode("utf-8"))
                if final_snap["status"] != "running":
                    break
                time.sleep(0.05)
            self.assertIsNotNone(final_snap)
            self.assertEqual(final_snap["status"], "completed")
            self.assertEqual(final_snap["failed"], 1)
            self.assertIn("unexpected boom", final_snap["errors"][0])
        finally:
            server.enrich_manager.cancel()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_enrich_missing_recomputes_local_area_and_queues_remaining(self) -> None:
        with AuctionDB(self.db_path) as db:
            # Make w_202_r2 missing area_sqm in properties, but give it notice_text containing area
            row_202 = db.conn.execute(
                "SELECT id, property_id FROM auction_rounds WHERE item_id = 'w_202_r2'"
            ).fetchone()
            db.conn.execute(
                "UPDATE properties SET area_sqm = NULL WHERE id = ?",
                (int(row_202["property_id"]),),
            )
            db.conn.execute(
                "UPDATE auction_rounds SET unit_price = NULL, notice_text = '建筑面积98.5平方米。' WHERE id = ?",
                (int(row_202["id"]),),
            )
            db.conn.commit()

        class _QuickStubProvider:
            platform = "taobao"

            def check_ready(self) -> tuple[bool, str]:
                return True, "ok"

            def fetch_detail(self, item_id: str, url: str) -> RawAuctionDetail:
                return RawAuctionDetail(
                    platform="taobao",
                    item_id=item_id,
                    url=url,
                    title="补抓成功",
                    area_sqm=120.0,
                    notice_text="建筑面积120平方米。",
                )

        def stub_factory(cfg: AppConfig) -> dict[str, AuctionProvider]:
            return {"taobao": _QuickStubProvider()}

        cfg = AppConfig(
            storage=StorageConfig(db_path=self.db_path),
            regions=[RegionConfig(name="福州市-住宅", province="福建", city="福州", location_code="350100")],
        )
        server = create_web_server(
            cfg,
            host="127.0.0.1",
            port=0,
            provider_factory=stub_factory,
            sleep_fn=lambda _: None,
        )
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            req = Request(
                f"{base}/api/enrich/missing",
                data=json.dumps({"query": "property_type=commercial", "limit": 30}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 202)
                body = json.loads(resp.read().decode("utf-8"))
            self.assertGreaterEqual(body["recomputed"]["areas_filled"], 1)
            self.assertEqual(body["queued"], 1)

            for _ in range(40):
                with urlopen(f"{base}/api/enrich/status", timeout=5) as resp:
                    snap = json.loads(resp.read().decode("utf-8"))
                if snap["status"] != "running":
                    break
                time.sleep(0.05)
            self.assertEqual(snap["status"], "completed")
            self.assertEqual(snap["succeeded"], 1)

            with AuctionDB(self.db_path, read_only=True) as db:
                p202 = db.conn.execute(
                    "SELECT area_sqm, unit_price FROM v_auction_full WHERE item_id = 'w_202_r2'"
                ).fetchone()
                self.assertEqual(p202["area_sqm"], 98.5)
                self.assertIsNotNone(p202["unit_price"])
        finally:
            server.enrich_manager.cancel()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_batch_enrich_platform_isolation_and_auto_resume(self) -> None:
        with AuctionDB(self.db_path, read_only=True) as db:
            tb_row = db.conn.execute("SELECT id FROM auction_rounds WHERE item_id = 'w_202_r2'").fetchone()
            jd_row = db.conn.execute("SELECT id FROM auction_rounds WHERE item_id = 'w_404_jd'").fetchone()
            tb_id = int(tb_row["id"])
            jd_id = int(jd_row["id"])

        call_order: list[str] = []

        class _TaobaoAuthStub:
            platform = "taobao"

            def __init__(self) -> None:
                self.cleared = False
                self.probe_calls = 0

            def check_ready(self) -> tuple[bool, str]:
                return True, "ok"

            def check_auth_cleared(self) -> bool:
                self.probe_calls += 1
                if self.probe_calls >= 2:
                    self.cleared = True
                return self.cleared

            def fetch_detail(self, item_id: str, url: str) -> RawAuctionDetail:
                call_order.append(f"taobao:{item_id}")
                if not self.cleared:
                    raise AuthRequiredError("taobao slider", url=url, task_space="tb-space")
                return RawAuctionDetail(
                    platform="taobao",
                    item_id=item_id,
                    url=url,
                    title="淘宝补抓成功",
                    area_sqm=92.0,
                    notice_text="建筑面积92平方米。",
                )

        class _JdOkStub:
            platform = "jd"

            def check_ready(self) -> tuple[bool, str]:
                return True, "ok"

            def fetch_detail(self, item_id: str, url: str) -> RawAuctionDetail:
                call_order.append(f"jd:{item_id}")
                return RawAuctionDetail(
                    platform="jd",
                    item_id=item_id,
                    url=url,
                    title="京东补抓成功",
                    area_sqm=115.0,
                    notice_text="建筑面积115平方米。",
                )

        tb_stub = _TaobaoAuthStub()
        jd_stub = _JdOkStub()

        cfg = AppConfig(
            storage=StorageConfig(db_path=self.db_path),
            regions=[RegionConfig(name="福州市-住宅", province="福建", city="福州", location_code="350100")],
        )
        server = create_web_server(
            cfg,
            host="127.0.0.1",
            port=0,
            provider_factory=lambda _c: {"taobao": tb_stub, "jd": jd_stub},
            sleep_fn=lambda _: None,
            auth_probe_interval=0.04,
        )
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            req = Request(
                f"{base}/api/enrich",
                data=json.dumps({"round_ids": [tb_id, jd_id], "force": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 202)

            final_snap = None
            for _ in range(80):
                with urlopen(f"{base}/api/enrich/status", timeout=5) as resp:
                    final_snap = json.loads(resp.read().decode("utf-8"))
                if final_snap["status"] in {"completed", "error", "cancelled"}:
                    break
                time.sleep(0.05)

            self.assertIsNotNone(final_snap)
            self.assertEqual(final_snap["status"], "completed")
            self.assertEqual(final_snap["succeeded"], 2)
            self.assertGreaterEqual(tb_stub.probe_calls, 2)
            # Taobao auth deferred -> JD ran before Taobao retried
            self.assertEqual(call_order, ["taobao:w_202_r2", "jd:w_404_jd", "taobao:w_202_r2"])
        finally:
            server.enrich_manager.cancel()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
