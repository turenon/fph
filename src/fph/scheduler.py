from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta, timezone

from fph.config import AppConfig
from fph.db import AuctionDB
from fph.pipeline import SyncPipeline, build_providers

logger = logging.getLogger(__name__)
CN_TZ = timezone(timedelta(hours=8))


def run_scheduler(config: AppConfig, *, run_immediately: bool = True) -> None:
    """Run periodic sync loop with randomized jitter (cross-platform macOS & Windows)."""
    interval_min = max(5, config.schedule.interval_minutes)
    jitter_min = max(0, config.schedule.jitter_minutes)

    logger.info(
        "启动定时调度器：基础间隔 %d 分钟，随机抖动 0~%d 分钟（Ctrl+C 退出）",
        interval_min,
        jitter_min,
    )

    first = True
    try:
        while True:
            if not first or run_immediately:
                try:
                    with AuctionDB(config.storage.db_path) as db:
                        providers = build_providers(config)
                        pipeline = SyncPipeline(config, db, providers=providers)
                        summaries = pipeline.run_full_cycle(
                            enrich_details=config.schedule.auto_enrich_details,
                            refresh_ended=config.schedule.auto_refresh_ended,
                        )
                        for s in summaries:
                            logger.info(
                                "[%s] status=%s pages=%d seen=%d filtered=%d skipped_old=%d skipped_existing=%d new_props=%d enriched=%d refreshed=%d %s",
                                s.region_name,
                                s.status,
                                s.pages_fetched,
                                s.items_seen,
                                s.items_filtered,
                                s.items_skipped_old,
                                s.items_skipped_existing,
                                s.properties_created,
                                s.details_enriched,
                                s.ended_refreshed,
                                s.message,
                            )
                except Exception:
                    logger.exception("本轮定时采集执行异常，将在下个周期继续重试")
            first = False

            wait_seconds = interval_min * 60 + random.randint(0, jitter_min * 60)
            next_time = datetime.now(CN_TZ) + timedelta(seconds=wait_seconds)
            logger.info("下一次采集时间: %s（等待 %.1f 分钟）", next_time.strftime("%Y-%m-%d %H:%M:%S"), wait_seconds / 60)
            time.sleep(wait_seconds)
    except KeyboardInterrupt:
        logger.info("收到中断信号，定时调度器已安全退出。")
