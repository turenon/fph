from __future__ import annotations

import argparse
import logging
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from fph.analytics import export_dataset, find_bargain_candidates, get_market_summary
from fph.config import (
    AppConfig,
    load_config,
    normalize_property_types,
    normalize_statuses,
    resolve_allowed_property_types,
)
from fph.db import AuctionDB, now_str
from fph.pipeline import (
    SyncPipeline,
    _cutoff_datetime_str,
    build_provider,
    build_providers,
    normalize_provider_name,
    prompt_user_for_auth,
    resolve_region_provider_name,
)
from fph.scheduler import run_scheduler
from fph.web import run_web_server


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


def _resolve_cli_property_types(args: argparse.Namespace) -> list[str] | None:
    if getattr(args, "only_residential", False):
        return ["residential"]
    raw = getattr(args, "property_types", None)
    if raw is not None:
        return normalize_property_types(raw)
    return None


def _cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _load_or_default(args.config)
    print(f"系统环境: {platform.system()} {platform.release()} | Python {platform.python_version()}")
    print(f"数据库路径: {cfg.storage.db_path.resolve()}")
    global_types = resolve_allowed_property_types(cfg)
    filter_desc = ", ".join(global_types) if global_types else "全部类型（未限制）"
    print(f"采集物业类型过滤: {filter_desc}")
    lookback_desc = f"最近 {cfg.provider.lookback_days} 天" if cfg.provider.lookback_days > 0 else "不限制"
    print(f"历史时间窗口: {lookback_desc}")
    enrich_statuses_desc = (
        ", ".join(cfg.provider.enrich_statuses) if cfg.provider.enrich_statuses else "全部状态（未限制）"
    )
    print(f"详情补抓状态: {enrich_statuses_desc}")
    print(f"已结束整页早停: {'开启' if cfg.provider.stop_on_known_ended_page else '关闭'}")
    refresh_window_desc = (
        f"{cfg.provider.ended_refresh_window_days} 天"
        if cfg.provider.ended_refresh_window_days > 0
        else "不限制"
    )
    print(
        f"重试策略: 详情最多 {cfg.provider.max_detail_attempts} 次 / "
        f"回刷最多 {cfg.provider.max_refresh_attempts} 次 / "
        f"冷却 {cfg.provider.retry_cooldown_hours} 小时 / "
        f"回刷窗口 {refresh_window_desc}"
    )
    print(
        f"断点回填策略: 首次回填单次最多 {cfg.provider.backfill_max_pages} 页 / "
        f"日常增量 {cfg.provider.max_pages_per_query} 页 / "
        f"验证弹窗等待 {cfg.provider.auth_wait_timeout_seconds} 秒"
    )
    if cfg.storage.db_path.exists() and cfg.storage.db_path.stat().st_size > 0:
        with AuctionDB(cfg.storage.db_path, read_only=True) as db:
            prop_cnt = db.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
            round_cnt = db.conn.execute("SELECT COUNT(*) FROM auction_rounds").fetchone()[0]
            print(f"当前入库统计: 标的物 {prop_cnt} 套 | 拍卖轮次 {round_cnt} 条")
            checkpoints = db.list_checkpoints()
            if checkpoints:
                print("回填断点进度:")
                for cp in checkpoints:
                    state = "已完成回填（增量模式）" if cp["backfill_completed"] else f"待从第 {cp['next_page']} 页续采 ({cp['last_status']})"
                    print(f"  - [{cp['provider']}] {cp['region_name']} [{cp['scope']}]: {state}")
    else:
        with AuctionDB(cfg.storage.db_path) as db:
            prop_cnt = db.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
            round_cnt = db.conn.execute("SELECT COUNT(*) FROM auction_rounds").fetchone()[0]
            print(f"当前入库统计: 标的物 {prop_cnt} 套 | 拍卖轮次 {round_cnt} 条")

    providers = build_providers(cfg)
    all_ok = True
    for pname, prov in providers.items():
        ok, msg = prov.check_ready()
        all_ok = all_ok and ok
        print(f"采集驱动 ({pname} / {prov.platform}): {'[OK]' if ok else '[WARN]'} {msg}")

    print("已配置监控区域与目标 URL 预览:")
    for region in cfg.regions:
        pname = resolve_region_provider_name(cfg, region)
        prov = providers[pname]
        override_note = ""
        if region.allowed_property_types is not None:
            eff_types = resolve_allowed_property_types(cfg, region)
            override_note = f" [类型过滤: {', '.join(eff_types) if eff_types else '全部类型'}]"
        for scope in region.status_scopes:
            for label, st_param in prov.expand_status_queries(scope):
                preview_url = prov.preview_list_url(region, st_param=st_param, page=1)
                print(f"  - [{prov.platform}] {region.name}{override_note} [{label}]: {preview_url}")
    return 0 if all_ok else 1


def _cmd_sync(args: argparse.Namespace) -> int:
    cfg = _load_or_default(args.config)
    try:
        cli_property_types = _resolve_cli_property_types(args)
        cli_enrich_statuses = (
            normalize_statuses(args.enrich_statuses)
            if args.enrich_statuses is not None
            else None
        )
        if args.lookback_days is not None and args.lookback_days < 0:
            raise ValueError("--lookback-days 不能为负数")
    except ValueError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2
    providers = build_providers(cfg)
    if args.provider:
        pnorm = normalize_provider_name(args.provider)
        if pnorm not in providers:
            providers[pnorm] = build_provider(cfg, pnorm)
    auth_handler = (
        None
        if (getattr(args, "no_wait_auth", False) or cfg.provider.auth_wait_timeout_seconds <= 0)
        else (lambda exc: prompt_user_for_auth(exc, timeout_seconds=cfg.provider.auth_wait_timeout_seconds))
    )
    with AuctionDB(cfg.storage.db_path) as db:
        if getattr(args, "reset_checkpoint", False):
            p_filter = normalize_provider_name(args.provider) if args.provider else None
            p_platform = {"taobao_ego": "taobao", "jd_ego": "jd"}.get(p_filter or "", p_filter)
            cleared = db.reset_checkpoints(provider=p_platform, region_name=args.region)
            print(f"已重置 {cleared} 条回填断点记录。")
        pipeline = SyncPipeline(cfg, db, providers=providers, auth_handler=auth_handler)
        summaries = pipeline.run_full_cycle(
            region_filter=args.region,
            provider_filter=args.provider,
            max_pages=args.max_pages,
            enrich_details=not args.no_enrich,
            refresh_ended=not args.no_refresh_ended,
            allowed_property_types=cli_property_types,
            active_only=args.active_only,
            lookback_days=args.lookback_days,
            enrich_statuses=cli_enrich_statuses,
        )

    exit_code = 0
    for s in summaries:
        print(
            f"[{s.provider}/{s.region_name}] 状态={s.status} | 抓取页数={s.pages_fetched} | "
            f"扫描条目={s.items_seen} | 过滤跳过={s.items_filtered} | "
            f"超期跳过={s.items_skipped_old} | 已终态跳过={s.items_skipped_existing} | "
            f"新增标的={s.properties_created} | "
            f"补抓详情={s.details_enriched} | 回刷结拍={s.ended_refreshed}"
        )
        if s.message:
            print(f"  提示: {s.message}")
        if s.status != "success":
            exit_code = 2
    return exit_code


def _cmd_enrich(args: argparse.Namespace) -> int:
    cfg = _load_or_default(args.config)
    try:
        cli_property_types = _resolve_cli_property_types(args)
        cli_statuses = normalize_statuses(args.statuses) if args.statuses is not None else None
        if args.lookback_days is not None and args.lookback_days < 0:
            raise ValueError("--lookback-days 不能为负数")
    except ValueError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2
    providers = build_providers(cfg)
    if args.provider:
        pnorm = normalize_provider_name(args.provider)
        if pnorm not in providers:
            providers[pnorm] = build_provider(cfg, pnorm)
    auth_handler = (
        None
        if (getattr(args, "no_wait_auth", False) or cfg.provider.auth_wait_timeout_seconds <= 0)
        else (lambda exc: prompt_user_for_auth(exc, timeout_seconds=cfg.provider.auth_wait_timeout_seconds))
    )
    with AuctionDB(cfg.storage.db_path) as db:
        pipeline = SyncPipeline(cfg, db, providers=providers, auth_handler=auth_handler)
        count, auth_err = pipeline.enrich_missing_details(
            limit=args.limit,
            provider_filter=args.provider,
            allowed_property_types=cli_property_types,
            allowed_statuses=cli_statuses,
            lookback_days=args.lookback_days,
        )
        print(f"本次补抓详情与公告完成: {count} 条")
        if auth_err:
            print(f"中断提示: {auth_err}")
            return 2
    return 0


def _cmd_cleanup(args: argparse.Namespace) -> int:
    cfg = _load_or_default(args.config)
    if args.lookback_days is not None and args.lookback_days < 0:
        print("参数错误: --lookback-days 不能为负数", file=sys.stderr)
        return 2

    eff_lookback = cfg.provider.lookback_days if args.lookback_days is None else args.lookback_days
    min_date = _cutoff_datetime_str(eff_lookback)
    allowed_types = None if args.keep_non_residential else (resolve_allowed_property_types(cfg) or None)
    reset_captcha = not args.no_reset_captcha
    dry_run = bool(args.dry_run)

    if dry_run and not (cfg.storage.db_path.exists() and cfg.storage.db_path.stat().st_size > 0):
        result: dict[str, Any] = {
            "expired_rounds": 0,
            "non_target_type_rounds": 0,
            "total_delete_rounds": 0,
            "orphan_properties": 0,
            "captcha_reset_rounds": 0,
            "revocation_fixed_rounds": 0,
            "dry_run": True,
        }
    else:
        use_read_only = dry_run and cfg.storage.db_path.exists() and cfg.storage.db_path.stat().st_size > 0
        with AuctionDB(cfg.storage.db_path, read_only=use_read_only) as db:
            result = db.cleanup_stale_data(
                min_date=min_date,
                allowed_property_types=allowed_types,
                reset_captcha_details=reset_captcha,
                dry_run=dry_run,
            )

    is_dry_run = bool(result.get("dry_run", dry_run))
    mode_label = "预览模式，未修改数据库" if is_dry_run else "已执行清理"
    action_prefix = "待" if is_dry_run else "已"
    cutoff_desc = f"早于 {min_date}（最近 {eff_lookback} 天）" if min_date else "未启用"
    type_desc = ", ".join(allowed_types) if allowed_types else "不限制（保留非目标物业类型）"

    print(f"历史数据清理 [{mode_label}] - {cfg.storage.db_path.resolve()}")
    print(
        f"  清理策略: 时间窗口={cutoff_desc} | 保留物业类型={type_desc} | "
        f"重置滑块详情={'是' if reset_captcha else '否'}"
    )
    print(f"  超期历史轮次: {result['expired_rounds']} 条")
    print(f"  非目标物业轮次: {result['non_target_type_rounds']} 条")
    print(f"  {action_prefix}删轮次合计（去重）: {result['total_delete_rounds']} 条")
    print(f"  {action_prefix}删孤立标的物: {result['orphan_properties']} 套")
    print(f"  {action_prefix}重置滑块污染详情: {result['captcha_reset_rounds']} 条")
    print(f"  {action_prefix}修复撤拍(revocation)状态: {result['revocation_fixed_rounds']} 条")
    if is_dry_run:
        print("提示: 当前为预览模式，未修改数据库；确认无误后去掉 --dry-run 执行实际清理。")
    else:
        print("清理完成，数据库已更新。")
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


def _cmd_web(args: argparse.Namespace) -> int:
    cfg = _load_or_default(args.config)
    try:
        run_web_server(
            cfg,
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
        )
    except RuntimeError as exc:
        print(f"启动失败: {exc}", file=sys.stderr)
        return 1
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
    p_sync.add_argument("--provider", help="仅同步指定采集平台（如 taobao / jd / taobao_ego / jd_ego）")
    p_sync.add_argument("--max-pages", type=int, help="覆盖单次最大翻页数")
    p_sync.add_argument("--no-enrich", action="store_true", help="本次跳过详情/公告补抓")
    p_sync.add_argument("--no-refresh-ended", action="store_true", help="本次跳过已结拍结果回刷")
    p_sync.add_argument("--active-only", action="store_true", help="仅同步正在进行与即将开始（跳过已结束列表）")
    p_sync.add_argument("--lookback-days", type=int, help="覆盖历史时间窗口天数（0 表示不限制）")
    p_sync.add_argument(
        "--enrich-statuses",
        help="覆盖自动补抓详情的状态列表，逗号分隔（如 ongoing,upcoming；传 all 表示不限制）",
    )
    p_sync.add_argument("--no-wait-auth", action="store_true", help="遇到滑块/登录时不弹窗等待，直接保存断点并退出")
    p_sync.add_argument("--reset-checkpoint", action="store_true", help="重置已结束列表的回填断点，从第 1 页重新回填")
    p_sync_types = p_sync.add_mutually_exclusive_group()
    p_sync_types.add_argument("--only-residential", action="store_true", help="本次仅采集与处理住宅类型（residential），覆盖配置")
    p_sync_types.add_argument(
        "--property-types",
        help="覆盖配置指定采集物业类型，逗号分隔（如 residential,commercial；传 all 表示不过滤）",
    )
    p_sync.set_defaults(func=_cmd_sync)

    p_enrich = sub.add_parser("enrich", help="单独补抓缺失详情与公告的标的")
    p_enrich.add_argument("--limit", type=int, default=20, help="本次最多补抓条数")
    p_enrich.add_argument("--provider", help="仅补抓指定采集平台（如 taobao / jd / taobao_ego / jd_ego）")
    p_enrich.add_argument(
        "--statuses",
        help="覆盖配置指定补抓状态列表，逗号分隔（如 ongoing,upcoming 或 sold；传 all 表示不限制）",
    )
    p_enrich.add_argument("--lookback-days", type=int, help="覆盖历史时间窗口天数（0 表示不限制）")
    p_enrich.add_argument("--no-wait-auth", action="store_true", help="遇到滑块/登录时不弹窗等待，直接退出")
    p_enrich_types = p_enrich.add_mutually_exclusive_group()
    p_enrich_types.add_argument("--only-residential", action="store_true", help="本次仅补抓住宅类型（residential），覆盖配置")
    p_enrich_types.add_argument(
        "--property-types",
        help="覆盖配置指定补抓物业类型，逗号分隔（如 residential,commercial；传 all 表示不过滤）",
    )
    p_enrich.set_defaults(func=_cmd_enrich)

    p_cleanup = sub.add_parser("cleanup", help="清理超期历史记录、非目标物业类型，并修复滑块污染与撤拍状态")
    p_cleanup.add_argument("--dry-run", action="store_true", help="仅预览待清理与修复统计，不修改数据库")
    p_cleanup.add_argument("--lookback-days", type=int, help="覆盖历史时间窗口天数（默认读取配置，0 表示不按时间清理）")
    p_cleanup.add_argument("--keep-non-residential", action="store_true", help="保留非目标物业类型记录，不按物业类型清理")
    p_cleanup.add_argument("--no-reset-captcha", action="store_true", help="不重置被滑块验证污染的详情记录")
    p_cleanup.set_defaults(func=_cmd_cleanup)

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

    p_web = sub.add_parser("web", help="启动本地 Web 数据看板（浏览器可视化查看与筛选）")
    p_web.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    p_web.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    p_web.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    p_web.set_defaults(func=_cmd_web)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
