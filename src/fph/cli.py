from __future__ import annotations

import argparse
import logging
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from fph.analytics import export_dataset, find_bargain_candidates, get_market_summary
from fph.config import AppConfig, load_config
from fph.db import AuctionDB, now_str
from fph.pipeline import SyncPipeline, build_provider
from fph.providers.taobao_urls import build_list_url, expand_status_queries
from fph.scheduler import run_scheduler


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _format_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    if not rows:
        return "（暂无数据）"

    headers = [label for _, label in columns]
    data_rows: list[list[str]] = []
    for r in rows:
        line: list[str] = []
        for key, _ in columns:
            val = r.get(key)
            if isinstance(val, list):
                text = ",".join(str(x) for x in val) if val else "-"
            elif val is None or val == "":
                text = "-"
            else:
                text = str(val)
            line.append(text)
        data_rows.append(line)

    widths = [len(h) for h in headers]
    for line in data_rows:
        for i, cell in enumerate(line):
            widths[i] = max(widths[i], min(len(cell), 48))

    def _pad(s: str, w: int) -> str:
        if len(s) > 48:
            s = s[:45] + "..."
        return s.ljust(w)

    sep = " | "
    out = [
        sep.join(_pad(h, widths[i]) for i, h in enumerate(headers)),
        "-+-".join("-" * w for w in widths),
    ]
    for line in data_rows:
        out.append(sep.join(_pad(c, widths[i]) for i, c in enumerate(line)))
    return "\n".join(out)


def _load_or_default(config_path: str | None) -> AppConfig:
    if config_path:
        return load_config(config_path)
    default_file = Path("config.toml")
    if default_file.exists():
        return load_config(default_file)
    return load_config(None)


def _cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.output)
    example = Path(__file__).resolve().parents[2] / "config.example.toml"
    if target.exists() and not args.force:
        print(f"配置文件已存在: {target}（如需覆盖请加 --force）")
    else:
        if example.exists():
            shutil.copyfile(example, target)
        else:
            target.write_text(
                '[storage]\ndb_path = "data/fph.db"\nexport_dir = "exports"\n',
                encoding="utf-8",
            )
        print(f"已生成配置文件: {target}")

    cfg = load_config(target)
    with AuctionDB(cfg.storage.db_path):
        pass
    print(f"已初始化 SQLite 分析库: {cfg.storage.db_path}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _load_or_default(args.config)
    print(f"系统环境: {platform.system()} {platform.release()} | Python {platform.python_version()}")
    print(f"数据库路径: {cfg.storage.db_path.resolve()}")
    with AuctionDB(cfg.storage.db_path) as db:
        prop_cnt = db.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
        round_cnt = db.conn.execute("SELECT COUNT(*) FROM auction_rounds").fetchone()[0]
        print(f"当前入库统计: 标的物 {prop_cnt} 套 | 拍卖轮次 {round_cnt} 条")

    provider = build_provider(cfg)
    ok, msg = provider.check_ready()
    print(f"采集驱动 ({cfg.provider.name}): {'[OK]' if ok else '[WARN]'} {msg}")

    print("已配置监控区域与目标 URL 预览:")
    for region in cfg.regions:
        for scope in region.status_scopes:
            for label, st_param in expand_status_queries(scope):
                print(f"  - {region.name} [{label}]: {build_list_url(region, st_param=st_param, page=1)}")
    return 0 if ok else 1


def _cmd_sync(args: argparse.Namespace) -> int:
    cfg = _load_or_default(args.config)
    provider = build_provider(cfg)
    with AuctionDB(cfg.storage.db_path) as db:
        pipeline = SyncPipeline(cfg, db, provider)
        summaries = pipeline.run_full_cycle(
            region_filter=args.region,
            max_pages=args.max_pages,
            enrich_details=not args.no_enrich,
            refresh_ended=not args.no_refresh_ended,
        )

    exit_code = 0
    for s in summaries:
        print(
            f"[{s.region_name}] 状态={s.status} | 抓取页数={s.pages_fetched} | "
            f"扫描条目={s.items_seen} | 新增标的={s.properties_created} | "
            f"补抓详情={s.details_enriched} | 回刷结拍={s.ended_refreshed}"
        )
        if s.message:
            print(f"  提示: {s.message}")
        if s.status != "success":
            exit_code = 2
    return exit_code


def _cmd_enrich(args: argparse.Namespace) -> int:
    cfg = _load_or_default(args.config)
    provider = build_provider(cfg)
    with AuctionDB(cfg.storage.db_path) as db:
        pipeline = SyncPipeline(cfg, db, provider)
        count, auth_err = pipeline.enrich_missing_details(limit=args.limit)
        print(f"本次补抓详情与公告完成: {count} 条")
        if auth_err:
            print(f"中断提示: {auth_err}")
            return 2
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    cfg = _load_or_default(args.config)
    with AuctionDB(cfg.storage.db_path) as db:
        rows = get_market_summary(
            db,
            city=args.city,
            by_district=args.by_district,
            by_month=args.by_month,
        )

    cols: list[tuple[str, str]] = [("city", "城市")]
    if args.by_district:
        cols.append(("district", "区县"))
    if args.by_month:
        cols.append(("month", "月份"))
    cols.extend(
        [
            ("unique_properties", "标的数"),
            ("total_rounds", "拍次数"),
            ("active_count", "在拍/预告"),
            ("sold_count", "成交"),
            ("failed_count", "流拍"),
            ("clearance_rate_pct", "成交率%"),
            ("avg_sold_discount", "成交均折"),
            ("avg_sold_unit_price", "成交均价(元/㎡)"),
            ("avg_active_unit_price", "在拍均价(元/㎡)"),
        ]
    )
    print(_format_table(rows, cols))
    return 0


def _cmd_bargains(args: argparse.Namespace) -> int:
    cfg = _load_or_default(args.config)
    with AuctionDB(cfg.storage.db_path) as db:
        rows = find_bargain_candidates(
            db,
            city=args.city,
            district=args.district,
            max_discount=args.max_discount,
            max_total_wan=args.max_total,
            min_area=args.min_area,
            max_risk_level=args.max_risk,
            stage=args.stage,
            limit=args.limit,
        )

    cols = [
        ("city", "城市"),
        ("district", "区县"),
        ("stage", "阶段"),
        ("price_wan", "当前/起拍(万)"),
        ("discount_rate", "折扣"),
        ("area_sqm", "面积(㎡)"),
        ("unit_price", "单价(元/㎡)"),
        ("apply_count", "报名"),
        ("risk_level", "风险"),
        ("risk_tags", "风险标签"),
        ("end_time", "结拍时间"),
        ("title", "标题"),
    ]
    print(_format_table(rows, cols))
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    cfg = _load_or_default(args.config)
    if args.output:
        out_path = Path(args.output)
    else:
        stamp = now_str().replace(":", "").replace(" ", "_").replace("-", "")
        city_part = f"_{args.city}" if args.city else ""
        out_path = cfg.storage.export_dir / f"fph_{args.dataset}{city_part}_{stamp}.{args.format}"

    with AuctionDB(cfg.storage.db_path) as db:
        count = export_dataset(
            db,
            out_path,
            dataset=args.dataset,
            city=args.city,
            fmt=args.format,
        )
    print(f"已导出 {count} 条记录到: {out_path}")
    return 0


def _cmd_schedule(args: argparse.Namespace) -> int:
    cfg = _load_or_default(args.config)
    if args.interval is not None:
        cfg.schedule.interval_minutes = args.interval
    run_scheduler(cfg, run_immediately=not args.delay_first)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fph",
        description="法拍房全生命周期分析库与捡漏监控工具（支持福州/龙岩，适配 ego-lite）",
    )
    parser.add_argument("-c", "--config", help="配置文件路径（默认读取当前目录 config.toml）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="生成默认 config.toml 并初始化 SQLite 数据库")
    p_init.add_argument("-o", "--output", default="config.toml", help="配置文件输出路径")
    p_init.add_argument("--force", action="store_true", help="覆盖已有配置文件")
    p_init.set_defaults(func=_cmd_init)

    p_doctor = sub.add_parser("doctor", help="检查本机环境、数据库与 ego-browser 状态")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_sync = sub.add_parser("sync", help="执行一次列表同步 + 新标的详情补抓 + 结拍结果回刷")
    p_sync.add_argument("--region", help="仅同步名称或城市包含指定关键字的区域（如 福州 / 龙岩）")
    p_sync.add_argument("--max-pages", type=int, help="覆盖单次最大翻页数")
    p_sync.add_argument("--no-enrich", action="store_true", help="本次跳过详情/公告补抓")
    p_sync.add_argument("--no-refresh-ended", action="store_true", help="本次跳过已结拍结果回刷")
    p_sync.set_defaults(func=_cmd_sync)

    p_enrich = sub.add_parser("enrich", help="单独补抓缺失详情与公告的标的")
    p_enrich.add_argument("--limit", type=int, default=20, help="本次最多补抓条数")
    p_enrich.set_defaults(func=_cmd_enrich)

    p_stats = sub.add_parser("stats", help="查看区域住宅法拍行情统计（成交率、均折、单价）")
    p_stats.add_argument("--city", help="按城市过滤（如 福州 / 龙岩）")
    p_stats.add_argument("--by-district", action="store_true", help="按区县细分")
    p_stats.add_argument("--by-month", action="store_true", help="按月份细分")
    p_stats.set_defaults(func=_cmd_stats)

    p_bargains = sub.add_parser("bargains", help="筛选当前在拍/预告的高性价比住宅捡漏候选")
    p_bargains.add_argument("--city", help="按城市过滤（如 福州 / 龙岩）")
    p_bargains.add_argument("--district", help="按区县过滤（如 鼓楼 / 仓山 / 新罗）")
    p_bargains.add_argument("--max-discount", type=float, default=0.80, help="最大折扣率（默认 0.80 即 8 折以内）")
    p_bargains.add_argument("--max-total", type=float, help="最高总价（万元）")
    p_bargains.add_argument("--min-area", type=float, help="最小建筑面积（㎡）")
    p_bargains.add_argument("--max-risk", choices=["low", "medium", "high"], default="medium", help="允许的最高风险等级")
    p_bargains.add_argument("--stage", help="按阶段过滤（一拍 / 二拍 / 变卖）")
    p_bargains.add_argument("--limit", type=int, default=30, help="最多显示条数")
    p_bargains.set_defaults(func=_cmd_bargains)

    p_export = sub.add_parser("export", help="导出数据为 CSV（Excel 友好）或 JSON")
    p_export.add_argument("--dataset", choices=["all", "deals", "bargains"], default="all", help="导出的数据视图")
    p_export.add_argument("--city", help="按城市过滤")
    p_export.add_argument("--format", choices=["csv", "json"], default="csv", help="导出格式")
    p_export.add_argument("-o", "--output", help="自定义输出文件路径")
    p_export.set_defaults(func=_cmd_export)

    p_sched = sub.add_parser("schedule", help="启动跨平台定时常驻采集")
    p_sched.add_argument("--interval", type=int, help="覆盖执行间隔（分钟）")
    p_sched.add_argument("--delay-first", action="store_true", help="启动后先等待一个周期再首次执行")
    p_sched.set_defaults(func=_cmd_schedule)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
