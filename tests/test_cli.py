from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fph.cli import build_parser, main
from fph.config import load_config, resolve_allowed_property_types
from fph.db import AuctionDB
from fph.models import RawAuctionDetail, RawAuctionItem
from fph.providers.base import AuctionProvider, PageResult

CN_TZ = timezone(timedelta(hours=8))


class _CliStubProvider(AuctionProvider):
    platform = "taobao"

    def __init__(self, list_items: list[RawAuctionItem] | None = None) -> None:
        self.list_items = list_items or []
        self.list_calls: list[tuple[str, int]] = []
        self.detail_calls: list[str] = []

    def check_ready(self) -> tuple[bool, str]:
        return True, "ok"

    def expand_status_queries(self, status_scope: str) -> list[tuple[str, str]]:
        mapping = {
            "active": [("ongoing", "0"), ("upcoming", "1")],
            "ongoing": [("ongoing", "0")],
            "upcoming": [("upcoming", "1")],
            "ended": [("ended", "2")],
            "all": [("all", "-1")],
        }
        return mapping.get(status_scope, [("all", "-1")])

    def fetch_list_page(self, region, status_scope: str, page: int) -> PageResult:
        self.list_calls.append((status_scope, page))
        items = self.list_items if status_scope == "0" and page == 1 else []
        return PageResult(items=items, page=page, has_next=False)

    def fetch_detail(self, item_id: str, url: str) -> RawAuctionDetail:
        self.detail_calls.append(item_id)
        return RawAuctionDetail(
            platform="taobao",
            item_id=item_id,
            url=url,
            notice_text="建筑面积95平方米，已腾空。",
        )


class CliTest(unittest.TestCase):
    def test_init_stats_bargains_export_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            prev_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                cfg_path = Path(tmpdir) / "config.toml"
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = main(["init", "-o", str(cfg_path)])
                self.assertEqual(rc, 0)
                self.assertTrue(cfg_path.exists())

                # Update db_path inside generated config to stay inside tmpdir
                content = cfg_path.read_text(encoding="utf-8").replace(
                    'db_path = "data/fph.db"',
                    f'db_path = "{(Path(tmpdir) / "fph.db").as_posix()}"',
                )
                cfg_path.write_text(content, encoding="utf-8")

                out_stats = io.StringIO()
                with redirect_stdout(out_stats):
                    self.assertEqual(main(["-c", str(cfg_path), "stats"]), 0)
                self.assertIn("暂无数据", out_stats.getvalue())

                out_bargains = io.StringIO()
                with redirect_stdout(out_bargains):
                    self.assertEqual(main(["-c", str(cfg_path), "bargains", "--city", "福州"]), 0)
                self.assertIn("暂无数据", out_bargains.getvalue())

                export_file = Path(tmpdir) / "out.csv"
                out_exp = io.StringIO()
                with redirect_stdout(out_exp):
                    self.assertEqual(
                        main(["-c", str(cfg_path), "export", "--dataset", "all", "-o", str(export_file)]),
                        0,
                    )
                self.assertTrue(export_file.exists())
            finally:
                os.chdir(prev_cwd)

    def test_allowed_property_types_config_doctor_and_cli_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.toml"
            db_path = (Path(tmpdir) / "fph.db").as_posix()
            cfg_path.write_text(
                f"""
[storage]
db_path = "{db_path}"

[provider]
allowed_property_types = ["residential"]

[[regions]]
name = "福州市-住宅"
province = "福建"
city = "福州"
location_code = "350100"

[[regions]]
name = "福州市-全类型"
province = "福建"
city = "福州"
location_code = "350100"
allowed_property_types = []
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(cfg_path)
            self.assertEqual(cfg.provider.allowed_property_types, ["residential"])
            self.assertIsNone(cfg.regions[0].allowed_property_types)
            self.assertEqual(cfg.regions[1].allowed_property_types, [])
            self.assertEqual(resolve_allowed_property_types(cfg, cfg.regions[0]), ("residential",))
            self.assertEqual(resolve_allowed_property_types(cfg, cfg.regions[1]), ())

            out_doc = io.StringIO()
            with redirect_stdout(out_doc):
                main(["-c", str(cfg_path), "doctor"])
            doc_text = out_doc.getvalue()
            self.assertIn("采集物业类型过滤: residential", doc_text)
            self.assertIn("福州市-全类型 [类型过滤: 全部类型]", doc_text)

            bad_cfg = Path(tmpdir) / "bad.toml"
            bad_cfg.write_text('[provider]\nallowed_property_types = ["villa_invalid"]\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(bad_cfg)

        parser = build_parser()
        args_only = parser.parse_args(["sync", "--only-residential"])
        self.assertTrue(args_only.only_residential)
        self.assertIsNone(args_only.property_types)

        args_multi = parser.parse_args(["sync", "--property-types", "residential,commercial"])
        self.assertEqual(args_multi.property_types, "residential,commercial")

        args_enrich = parser.parse_args(["enrich", "--property-types", "all"])
        self.assertEqual(args_enrich.property_types, "all")

        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["sync", "--only-residential", "--property-types", "residential"])

    def test_optimization_config_validation_and_doctor_display(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.toml"
            db_path = (Path(tmpdir) / "fph.db").as_posix()
            cfg_path.write_text(
                f"""
[storage]
db_path = "{db_path}"

[provider]
allowed_property_types = ["residential"]
lookback_days = 730
enrich_statuses = ["ongoing", "upcoming"]
stop_on_known_ended_page = true
max_detail_attempts = 4
max_refresh_attempts = 5
retry_cooldown_hours = 6
ended_refresh_window_days = 45

[[regions]]
name = "福州市-住宅"
province = "福建"
city = "福州"
location_code = "350100"
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(cfg_path)
            self.assertEqual(cfg.provider.lookback_days, 730)
            self.assertEqual(cfg.provider.enrich_statuses, ["ongoing", "upcoming"])
            self.assertTrue(cfg.provider.stop_on_known_ended_page)
            self.assertEqual(cfg.provider.max_detail_attempts, 4)
            self.assertEqual(cfg.provider.max_refresh_attempts, 5)
            self.assertEqual(cfg.provider.retry_cooldown_hours, 6)
            self.assertEqual(cfg.provider.ended_refresh_window_days, 45)

            out_doc = io.StringIO()
            with redirect_stdout(out_doc):
                main(["-c", str(cfg_path), "doctor"])
            doc_text = out_doc.getvalue()
            self.assertIn("历史时间窗口: 最近 730 天", doc_text)
            self.assertIn("详情补抓状态: ongoing, upcoming", doc_text)
            self.assertIn("已结束整页早停: 开启", doc_text)
            self.assertIn("重试策略: 详情最多 4 次 / 回刷最多 5 次 / 冷却 6 小时 / 回刷窗口 45 天", doc_text)

            # Invalid config values must fail fast
            invalid_snippets = [
                '[provider]\nenrich_statuses = ["bogus_status"]\n',
                "[provider]\nlookback_days = -1\n",
                "[provider]\nmax_detail_attempts = 0\n",
                "[provider]\nmax_refresh_attempts = 0\n",
                "[provider]\nretry_cooldown_hours = -2\n",
                "[provider]\nended_refresh_window_days = -3\n",
            ]
            for idx, snippet in enumerate(invalid_snippets):
                bad_cfg = Path(tmpdir) / f"bad_{idx}.toml"
                bad_cfg.write_text(snippet, encoding="utf-8")
                with self.assertRaises(ValueError, msg=f"Expected ValueError for snippet #{idx}"):
                    load_config(bad_cfg)

    def test_sync_and_enrich_new_cli_flags(self) -> None:
        parser = build_parser()
        sync_args = parser.parse_args(
            [
                "sync",
                "--active-only",
                "--lookback-days",
                "365",
                "--enrich-statuses",
                "all",
                "--no-wait-auth",
                "--reset-checkpoint",
            ]
        )
        self.assertTrue(sync_args.active_only)
        self.assertEqual(sync_args.lookback_days, 365)
        self.assertEqual(sync_args.enrich_statuses, "all")
        self.assertTrue(sync_args.no_wait_auth)
        self.assertTrue(sync_args.reset_checkpoint)

        enrich_args = parser.parse_args(["enrich", "--statuses", "sold", "--lookback-days", "180"])
        self.assertEqual(enrich_args.statuses, "sold")
        self.assertEqual(enrich_args.lookback_days, 180)

        recent_dt = (datetime.now(CN_TZ) - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        future_dt = (datetime.now(CN_TZ) + timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.toml"
            db_path = Path(tmpdir) / "fph.db"
            cfg_path.write_text(
                f"""
[storage]
db_path = "{db_path.as_posix()}"

[provider]
min_delay_seconds = 0
max_delay_seconds = 0
allowed_property_types = ["residential"]
lookback_days = 730
enrich_statuses = ["ongoing", "upcoming"]

[[regions]]
name = "福州市-住宅"
province = "福建"
city = "福州"
location_code = "350100"
status_scopes = ["active", "ended"]
""".strip(),
                encoding="utf-8",
            )

            # Seed one sold round needing detail so --enrich-statuses all can enrich it
            with AuctionDB(db_path) as db:
                db.upsert_list_item(
                    RawAuctionItem(
                        platform="taobao",
                        item_id="seed_sold_1",
                        url="https://sf-item.taobao.com/sf_item/seed_sold_1.htm",
                        title="【二拍】福州市鼓楼区温泉花园1幢101室住宅",
                        province="福建",
                        city="福州",
                        status="sold",
                        bid_count=2,
                        end_time=recent_dt,
                        category_hint="50025969",
                    )
                )

            stub_sync = _CliStubProvider(
                list_items=[
                    RawAuctionItem(
                        platform="taobao",
                        item_id="live_1",
                        url="https://sf-item.taobao.com/sf_item/live_1.htm",
                        title="【一拍】福州市鼓楼区温泉花园2幢202室住宅",
                        province="福建",
                        city="福州",
                        status="ongoing",
                        end_time=future_dt,
                        category_hint="50025969",
                    )
                ]
            )
            out_sync = io.StringIO()
            with (
                patch("fph.cli.build_providers", return_value={"taobao_ego": stub_sync}),
                redirect_stdout(out_sync),
            ):
                rc = main(
                    [
                        "-c",
                        str(cfg_path),
                        "sync",
                        "--active-only",
                        "--lookback-days",
                        "365",
                        "--enrich-statuses",
                        "all",
                    ]
                )
            self.assertEqual(rc, 0)
            sync_text = out_sync.getvalue()
            self.assertIn("超期跳过=0", sync_text)
            self.assertIn("已终态跳过=0", sync_text)
            # --active-only must not query ended ("2")
            self.assertEqual(stub_sync.list_calls, [("0", 1), ("1", 1)])
            # --enrich-statuses all must enrich both live_1 (ongoing) and seed_sold_1 (sold)
            self.assertEqual(set(stub_sync.detail_calls), {"live_1", "seed_sold_1"})

            # Seed two new items (one ongoing, one sold) and run `fph enrich --statuses sold`
            with AuctionDB(db_path) as db:
                db.upsert_list_item(
                    RawAuctionItem(
                        platform="taobao",
                        item_id="enr_ongoing",
                        url="https://sf-item.taobao.com/sf_item/enr_ongoing.htm",
                        title="【一拍】福州市仓山区金山花园3幢301室住宅",
                        province="福建",
                        city="福州",
                        status="ongoing",
                        end_time=future_dt,
                        category_hint="50025969",
                    )
                )
                db.upsert_list_item(
                    RawAuctionItem(
                        platform="taobao",
                        item_id="enr_sold",
                        url="https://sf-item.taobao.com/sf_item/enr_sold.htm",
                        title="【二拍】福州市仓山区金山花园4幢401室住宅",
                        province="福建",
                        city="福州",
                        status="sold",
                        bid_count=3,
                        end_time=recent_dt,
                        category_hint="50025969",
                    )
                )

            stub_enrich = _CliStubProvider()
            out_enrich = io.StringIO()
            with (
                patch("fph.cli.build_providers", return_value={"taobao_ego": stub_enrich}),
                redirect_stdout(out_enrich),
            ):
                rc_enr = main(["-c", str(cfg_path), "enrich", "--statuses", "sold"])
            self.assertEqual(rc_enr, 0)
            self.assertEqual(stub_enrich.detail_calls, ["enr_sold"])

            # Invalid status returns exit code 2
            err_buf = io.StringIO()
            with redirect_stderr(err_buf):
                self.assertEqual(main(["-c", str(cfg_path), "enrich", "--statuses", "invalid_xyz"]), 2)

    def test_cleanup_cli_dry_run_and_execute(self) -> None:
        recent_dt = (datetime.now(CN_TZ) - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        future_dt = (datetime.now(CN_TZ) + timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.toml"
            db_path = Path(tmpdir) / "cleanup.db"
            cfg_path.write_text(
                f"""
[storage]
db_path = "{db_path.as_posix()}"

[provider]
allowed_property_types = ["residential"]
lookback_days = 730

[[regions]]
name = "福州市-住宅"
province = "福建"
city = "福州"
location_code = "350100"
""".strip(),
                encoding="utf-8",
            )

            with AuctionDB(db_path) as db:
                # 1. Expired historical round (2015)
                db.upsert_list_item(
                    RawAuctionItem(
                        platform="taobao",
                        item_id="old_2015",
                        url="https://sf-item.taobao.com/sf_item/old_2015.htm",
                        title="【一拍】福州市鼓楼区老小区1幢101室住宅",
                        province="福建",
                        city="福州",
                        status="sold",
                        bid_count=2,
                        end_time="2015-06-01 10:00:00",
                        category_hint="50025969",
                    )
                )
                # 2. Non-residential recent round (parking)
                db.upsert_list_item(
                    RawAuctionItem(
                        platform="taobao",
                        item_id="park_1",
                        url="https://sf-item.taobao.com/sf_item/park_1.htm",
                        title="福州市鼓楼区温泉花园地下一层B01车位",
                        province="福建",
                        city="福州",
                        status="ongoing",
                        end_time=future_dt,
                        category_hint="50025969",
                    )
                )
                # 3. Recent residential round polluted by captcha
                db.upsert_list_item(
                    RawAuctionItem(
                        platform="taobao",
                        item_id="captcha_1",
                        url="https://sf-item.taobao.com/sf_item/captcha_1.htm",
                        title="【二拍】福州市仓山区橘园洲花园5幢501室住宅",
                        province="福建",
                        city="福州",
                        status="ongoing",
                        end_time=future_dt,
                        category_hint="50025969",
                    )
                )
                db.apply_detail(
                    RawAuctionDetail(
                        platform="taobao",
                        item_id="captcha_1",
                        url="https://sf-item.taobao.com/sf_item/captcha_1.htm",
                        notice_text="请拖动下方滑块完成验证以确保正常访问",
                    )
                )
                # 4. Recent residential round stuck as unknown with raw revocation status
                db.upsert_list_item(
                    RawAuctionItem(
                        platform="taobao",
                        item_id="rev_1",
                        url="https://sf-item.taobao.com/sf_item/rev_1.htm",
                        title="【一拍】福州市晋安区五四北花园6幢601室住宅",
                        province="福建",
                        city="福州",
                        status="unknown",
                        end_time=recent_dt,
                        category_hint="50025969",
                        raw_payload={"id": "rev_1", "status": "revocation"},
                    )
                )
                # Force status='unknown' & raw_json with "revocation" in case normalizer already maps it
                db.conn.execute(
                    "UPDATE auction_rounds SET status = 'unknown', raw_json = ? WHERE item_id = 'rev_1'",
                    (json.dumps({"id": "rev_1", "status": "revocation"}),),
                )
                db.conn.commit()
                # 5. Clean active residential round
                db.upsert_list_item(
                    RawAuctionItem(
                        platform="taobao",
                        item_id="clean_1",
                        url="https://sf-item.taobao.com/sf_item/clean_1.htm",
                        title="【二拍】福州市鼓楼区五四路花园7幢701室住宅",
                        province="福建",
                        city="福州",
                        status="upcoming",
                        start_time=future_dt,
                        category_hint="50025969",
                    )
                )

            # Dry-run preview must report accurate counts without touching DB
            out_dry = io.StringIO()
            with redirect_stdout(out_dry):
                rc_dry = main(["-c", str(cfg_path), "cleanup", "--dry-run"])
            self.assertEqual(rc_dry, 0)
            dry_text = out_dry.getvalue()
            self.assertIn("预览模式，未修改数据库", dry_text)
            self.assertIn("超期历史轮次: 1 条", dry_text)
            self.assertIn("非目标物业轮次: 1 条", dry_text)
            self.assertIn("待删轮次合计（去重）: 2 条", dry_text)
            self.assertIn("待删孤立标的物: 2 套", dry_text)
            self.assertIn("待重置滑块污染详情: 1 条", dry_text)
            self.assertIn("待修复撤拍(revocation)状态: 1 条", dry_text)

            with AuctionDB(db_path) as db:
                self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM auction_rounds").fetchone()[0], 5)
                self.assertEqual(db.get_round_status("taobao", "rev_1"), "unknown")

            # Dry-run with --keep-non-residential --no-reset-captcha
            out_flags = io.StringIO()
            with redirect_stdout(out_flags):
                rc_flags = main(
                    [
                        "-c",
                        str(cfg_path),
                        "cleanup",
                        "--dry-run",
                        "--keep-non-residential",
                        "--no-reset-captcha",
                    ]
                )
            self.assertEqual(rc_flags, 0)
            flags_text = out_flags.getvalue()
            self.assertIn("非目标物业轮次: 0 条", flags_text)
            self.assertIn("待重置滑块污染详情: 0 条", flags_text)

            # Real cleanup execution
            out_real = io.StringIO()
            with redirect_stdout(out_real):
                rc_real = main(["-c", str(cfg_path), "cleanup"])
            self.assertEqual(rc_real, 0)
            real_text = out_real.getvalue()
            self.assertIn("已执行清理", real_text)
            self.assertIn("清理完成", real_text)

            with AuctionDB(db_path) as db:
                remaining_ids = {
                    r[0] for r in db.conn.execute("SELECT item_id FROM auction_rounds").fetchall()
                }
                self.assertEqual(remaining_ids, {"captcha_1", "rev_1", "clean_1"})
                self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0], 3)
                cap_row = db.conn.execute(
                    "SELECT detail_fetched_at, notice_text FROM auction_rounds WHERE item_id = 'captcha_1'"
                ).fetchone()
                self.assertIsNone(cap_row["detail_fetched_at"])
                self.assertEqual(cap_row["notice_text"], "")
                self.assertEqual(db.get_round_status("taobao", "rev_1"), "withdrawn")

    def test_cli_sync_short_lookback_preserves_backfill_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.toml"
            db_path = Path(tmpdir) / "sync_cp.db"
            cfg_path.write_text(
                f"""
[storage]
db_path = "{db_path.as_posix()}"

[provider]
lookback_days = 730
max_pages_per_query = 1
backfill_max_pages = 50

[[regions]]
name = "福州市-住宅"
province = "福建"
city = "福州"
location_code = "350100"
status_scopes = ["ended"]
""".strip(),
                encoding="utf-8",
            )
            with AuctionDB(db_path) as db:
                db.save_checkpoint(
                    "taobao",
                    "福州市-住宅",
                    "ended:2",
                    next_page=5,
                    backfill_completed=False,
                    last_status="success",
                )

            class _FakeTaobao:
                platform = "taobao"

                def expand_status_queries(self, scope: str):
                    return [("ended", "2")]

                def fetch_list_page(self, region, *, status_scope: str = "", page: int = 1):
                    from fph.providers.base import PageResult
                    self_pages.append(page)
                    return PageResult(items=[], page=page, total_pages=1, has_next=False)

            self_pages: list[int] = []
            with patch("fph.cli.build_providers", return_value={"taobao_ego": _FakeTaobao()}):
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = main(["-c", str(cfg_path), "sync", "--no-enrich", "--no-refresh-ended", "--lookback-days", "7"])
            self.assertEqual(rc, 0)
            self.assertEqual(self_pages, [1])
            with AuctionDB(db_path) as db:
                cp = db.get_checkpoint("taobao", "福州市-住宅", "ended:2")
                self.assertEqual(cp["next_page"], 5)
                self.assertEqual(cp["backfill_completed"], 0)


if __name__ == "__main__":
    unittest.main()
