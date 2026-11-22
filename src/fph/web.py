from __future__ import annotations

import csv
import io
import json
import logging
import os
import random
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from fph.analytics import find_bargain_candidates, get_market_summary
from fph.config import AppConfig
from fph.db import AuctionDB, escape_like, now_str
from fph.pipeline import build_provider, build_providers, resolve_region_provider_name
from fph.providers.base import AuctionProvider, AuthRequiredError, ProviderError

logger = logging.getLogger(__name__)

_UI_HTML_PATH = Path(__file__).with_name("web_ui.html")
MAX_BATCH_ENRICH_SIZE = 30
_BROWSER_CONSOLE_ACTIONS = {"sync", "enrich", "schedule"}
_ALLOWED_CONSOLE_ACTIONS = {"sync", "enrich", "doctor", "cleanup", "schedule"}
_ALLOWED_CLI_PROVIDERS = {"taobao", "jd", "taobao_ego", "jd_ego"}
_AUTH_LOG_HINTS = ("auth_required", "滑块", "需要登录", "人机验证", "安全验证")

_SORT_SQL_MAP: dict[str, str] = {
    "end_time_desc": "COALESCE(end_time, start_time, last_seen_at) DESC, round_id DESC",
    "end_time_asc": "COALESCE(end_time, start_time, last_seen_at) ASC, round_id ASC",
    "price_asc": "CASE WHEN COALESCE(current_price, initial_price) IS NULL THEN 1 ELSE 0 END, COALESCE(current_price, initial_price) ASC, round_id DESC",
    "price_desc": "CASE WHEN COALESCE(current_price, initial_price) IS NULL THEN 1 ELSE 0 END, COALESCE(current_price, initial_price) DESC, round_id DESC",
    "discount_asc": "CASE WHEN discount_rate IS NULL THEN 1 ELSE 0 END, discount_rate ASC, round_id DESC",
    "unit_price_asc": "CASE WHEN unit_price IS NULL THEN 1 ELSE 0 END, unit_price ASC, round_id DESC",
    "watch_desc": "COALESCE(watch_count, -1) DESC, round_id DESC",
    "last_seen_desc": "last_seen_at DESC, round_id DESC",
}

_DATASET_VIEW_MAP: dict[str, str] = {
    "all": "v_auction_full",
    "deals": "v_residential_deals",
    "bargains": "v_bargain_candidates",
}


def _first_param(params: dict[str, list[str]], key: str, default: str = "") -> str:
    vals = params.get(key)
    if not vals:
        return default
    return vals[0].strip()


def _parse_bool(val: str) -> bool:
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(val: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if not val:
        result = default
    else:
        try:
            result = int(val)
        except ValueError as exc:
            raise ValueError(f"无效的整数参数: {val}") from exc
    if minimum is not None and result < minimum:
        result = minimum
    if maximum is not None and result > maximum:
        result = maximum
    return result


def _parse_optional_float(val: str) -> float | None:
    if not val:
        return None
    try:
        return float(val)
    except ValueError as exc:
        raise ValueError(f"无效的数值参数: {val}") from exc


def _row_with_tags(row: Any) -> dict[str, Any]:
    item = dict(row)
    raw_tags = item.pop("risk_tags_json", "[]")
    try:
        parsed = json.loads(raw_tags or "[]")
        item["risk_tags"] = parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        item["risk_tags"] = []
    return item


def _query_overview(db: AuctionDB, db_path: Path) -> dict[str, Any]:
    counts_row = db.conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM properties) AS properties_total,
            COUNT(*) AS rounds_total,
            SUM(CASE WHEN property_type = 'residential' AND is_partial_share = 0 THEN 1 ELSE 0 END) AS residential_rounds,
            SUM(CASE WHEN status IN ('upcoming', 'ongoing') THEN 1 ELSE 0 END) AS active_rounds,
            SUM(CASE WHEN status = 'sold' THEN 1 ELSE 0 END) AS sold_rounds,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_rounds,
            SUM(CASE WHEN detail_fetched_at IS NOT NULL THEN 1 ELSE 0 END) AS enriched_rounds
        FROM v_auction_full
        """
    ).fetchone()

    prop_types = [
        {"type": r["property_type"], "count": r["cnt"]}
        for r in db.conn.execute(
            "SELECT property_type, COUNT(*) AS cnt FROM v_auction_full GROUP BY property_type ORDER BY cnt DESC, property_type ASC"
        ).fetchall()
    ]
    statuses = [
        {"status": r["status"], "count": r["cnt"]}
        for r in db.conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM v_auction_full GROUP BY status ORDER BY cnt DESC, status ASC"
        ).fetchall()
    ]
    cities = [
        r["city"]
        for r in db.conn.execute(
            "SELECT DISTINCT city FROM v_auction_full WHERE city IS NOT NULL AND city != '' ORDER BY city ASC"
        ).fetchall()
    ]
    districts = [
        {"city": r["city"], "district": r["district"], "count": r["cnt"]}
        for r in db.conn.execute(
            """
            SELECT city, district, COUNT(*) AS cnt
            FROM v_auction_full
            WHERE district IS NOT NULL AND district != ''
            GROUP BY city, district
            ORDER BY city ASC, cnt DESC, district ASC
            """
        ).fetchall()
    ]
    stages = [
        r["stage"]
        for r in db.conn.execute(
            "SELECT DISTINCT stage FROM v_auction_full WHERE stage IS NOT NULL AND stage != '' ORDER BY stage ASC"
        ).fetchall()
    ]
    platforms = [
        {"platform": r["platform"], "count": r["cnt"]}
        for r in db.conn.execute(
            "SELECT platform, COUNT(*) AS cnt FROM v_auction_full GROUP BY platform ORDER BY cnt DESC, platform ASC"
        ).fetchall()
    ]

    return {
        "db_path": str(db_path.resolve()),
        "counts": {
            "properties_total": int(counts_row["properties_total"] or 0),
            "rounds_total": int(counts_row["rounds_total"] or 0),
            "residential_rounds": int(counts_row["residential_rounds"] or 0),
            "active_rounds": int(counts_row["active_rounds"] or 0),
            "sold_rounds": int(counts_row["sold_rounds"] or 0),
            "failed_rounds": int(counts_row["failed_rounds"] or 0),
            "enriched_rounds": int(counts_row["enriched_rounds"] or 0),
        },
        "platforms": platforms,
        "property_types": prop_types,
        "statuses": statuses,
        "cities": cities,
        "districts": districts,
        "stages": stages,
    }


def _query_items(db: AuctionDB, params: dict[str, list[str]]) -> dict[str, Any]:
    q = _first_param(params, "q")
    platform = _first_param(params, "platform", "all")
    city = _first_param(params, "city")
    district = _first_param(params, "district")
    property_type = _first_param(params, "property_type", "all")
    status = _first_param(params, "status", "all")
    stage = _first_param(params, "stage", "all")
    exclude_stage = _first_param(params, "exclude_stage")
    risk_level = _first_param(params, "risk_level", "all")
    only_full_share = _parse_bool(_first_param(params, "only_full_share"))
    only_enriched = _parse_bool(_first_param(params, "only_enriched"))
    max_discount = _parse_optional_float(_first_param(params, "max_discount"))
    sort_key = _first_param(params, "sort", "end_time_desc")
    page = _parse_int(_first_param(params, "page"), 1, minimum=1)
    page_size = _parse_int(_first_param(params, "page_size"), 20, minimum=1, maximum=200)

    where_clauses: list[str] = ["1=1"]
    sql_params: list[Any] = []

    if q:
        like_q = f"%{escape_like(q)}%"
        where_clauses.append(
            "(title LIKE ? ESCAPE '\\' OR address LIKE ? ESCAPE '\\' OR community_name LIKE ? ESCAPE '\\' "
            "OR court_name LIKE ? ESCAPE '\\' OR item_id LIKE ? ESCAPE '\\')"
        )
        sql_params.extend([like_q, like_q, like_q, like_q, like_q])
    if platform and platform != "all":
        where_clauses.append("platform = ?")
        sql_params.append(platform)
    if city:
        where_clauses.append("city LIKE ? ESCAPE '\\'")
        sql_params.append(f"%{escape_like(city.replace('市', ''))}%")
    if district:
        where_clauses.append("district LIKE ? ESCAPE '\\'")
        sql_params.append(f"%{escape_like(district)}%")
    if property_type and property_type != "all":
        where_clauses.append("property_type = ?")
        sql_params.append(property_type)
    if status and status != "all":
        if status == "active":
            where_clauses.append("status IN ('upcoming', 'ongoing')")
        else:
            where_clauses.append("status = ?")
            sql_params.append(status)
    if stage and stage != "all":
        stage_vals = [s.strip() for s in stage.split(",") if s.strip()]
        if len(stage_vals) == 1:
            where_clauses.append("stage = ?")
            sql_params.append(stage_vals[0])
        elif stage_vals:
            placeholders = ",".join("?" for _ in stage_vals)
            where_clauses.append(f"stage IN ({placeholders})")
            sql_params.extend(stage_vals)
    if exclude_stage:
        ex_vals = [s.strip() for s in exclude_stage.split(",") if s.strip()]
        if ex_vals:
            placeholders = ",".join("?" for _ in ex_vals)
            where_clauses.append(f"stage NOT IN ({placeholders})")
            sql_params.extend(ex_vals)
    if risk_level and risk_level != "all":
        where_clauses.append("risk_level = ?")
        sql_params.append(risk_level)
    if only_full_share:
        where_clauses.append("is_partial_share = 0")
    if only_enriched:
        where_clauses.append("detail_fetched_at IS NOT NULL")
    if max_discount is not None:
        where_clauses.append("discount_rate IS NOT NULL AND discount_rate <= ?")
        sql_params.append(max_discount)

    where_sql = " AND ".join(where_clauses)
    order_sql = _SORT_SQL_MAP.get(sort_key, _SORT_SQL_MAP["end_time_desc"])

    total = int(
        db.conn.execute(
            f"SELECT COUNT(*) FROM v_auction_full WHERE {where_sql}",
            sql_params,
        ).fetchone()[0]
    )

    offset = (page - 1) * page_size
    rows = db.conn.execute(
        f"""
        SELECT *
        FROM v_auction_full
        WHERE {where_sql}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
        """,
        sql_params + [page_size, offset],
    ).fetchall()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_row_with_tags(r) for r in rows],
    }


def _query_item_detail(db: AuctionDB, round_id: int) -> dict[str, Any] | None:
    row = db.conn.execute(
        """
        SELECT v.*, r.notice_text, r.survey_text, r.first_seen_at
        FROM v_auction_full v
        JOIN auction_rounds r ON r.id = v.round_id
        WHERE v.round_id = ?
        """,
        (round_id,),
    ).fetchone()
    if row is None:
        return None

    item = _row_with_tags(row)
    related_rows = db.conn.execute(
        """
        SELECT
            id AS round_id,
            platform,
            item_id,
            url,
            title,
            stage,
            status,
            initial_price,
            current_price,
            consult_price,
            discount_rate,
            unit_price,
            bid_count,
            apply_count,
            watch_count,
            start_time,
            end_time,
            detail_fetched_at
        FROM auction_rounds
        WHERE property_id = ?
        ORDER BY COALESCE(start_time, first_seen_at) ASC, id ASC
        """,
        (item["property_id"],),
    ).fetchall()

    snapshot_rows = db.conn.execute(
        """
        SELECT id, captured_at, status, current_price, bid_count, apply_count, watch_count
        FROM auction_snapshots
        WHERE round_id = ?
        ORDER BY captured_at ASC, id ASC
        """,
        (round_id,),
    ).fetchall()

    item["related_rounds"] = [dict(r) for r in related_rows]
    item["snapshots"] = [dict(s) for s in snapshot_rows]
    return item


def _query_sync_runs(db: AuctionDB) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        """
        SELECT id, provider, region_name, started_at, finished_at, status,
               pages_fetched, items_seen, properties_created, rounds_upserted,
               details_enriched, ended_refreshed, message
        FROM sync_runs
        ORDER BY id DESC
        LIMIT 30
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _build_export_payload(
    db: AuctionDB,
    *,
    dataset: str,
    city: str | None,
    fmt: str,
) -> tuple[bytes, str, str]:
    if dataset not in _DATASET_VIEW_MAP:
        raise ValueError(f"不支持的数据集: {dataset}（可选: all / deals / bargains）")
    if fmt not in {"csv", "json"}:
        raise ValueError(f"不支持的导出格式: {fmt}（可选: csv / json）")

    where_sql = ""
    params: list[Any] = []
    if city:
        where_sql = "WHERE city LIKE ? ESCAPE '\\'"
        params.append(f"%{escape_like(city.replace('市', ''))}%")

    sql = f"SELECT * FROM {_DATASET_VIEW_MAP[dataset]} {where_sql} ORDER BY COALESCE(end_time, last_seen_at) DESC"
    rows = [dict(r) for r in db.conn.execute(sql, params).fetchall()]
    filename = f"fph_{dataset}.{fmt}"

    if fmt == "json":
        body = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
        return body, "application/json; charset=utf-8", filename

    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    # utf-8-sig ensures Excel on Windows opens Chinese text without mojibake
    body = buf.getvalue().encode("utf-8-sig")
    return body, "text/csv; charset=utf-8", filename


class JobConflictError(RuntimeError):
    """Raised when attempting to start a batch enrich job while another is active."""


class EnrichJobManager:
    """Single-worker background queue for batch detail enrichment from the Web UI."""

    def __init__(
        self,
        config: AppConfig,
        *,
        provider_factory: Callable[[AppConfig], dict[str, AuctionProvider]] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        is_console_busy_cb: Callable[[], bool] | None = None,
        browser_lock: threading.Lock | None = None,
    ) -> None:
        self._config = config
        self._provider_factory = provider_factory or build_providers
        self._sleep = sleep_fn
        self._is_console_busy_cb = is_console_busy_cb
        self._browser_lock = browser_lock or threading.Lock()
        self._cond = threading.Condition()
        self._job: dict[str, Any] = {
            "job_id": "",
            "status": "idle",  # idle / running / auth_required / completed / cancelled / error
            "total": 0,
            "processed": 0,
            "succeeded": 0,
            "skipped": 0,
            "failed": 0,
            "force": False,
            "current_round_id": None,
            "current_item_id": "",
            "current_title": "",
            "auth_message": "",
            "auth_url": "",
            "task_space": config.provider.task_space,
            "started_at": "",
            "finished_at": "",
            "errors": [],
        }
        self._pending: list[dict[str, Any]] = []

    def _snapshot_locked(self) -> dict[str, Any]:
        snap = dict(self._job)
        snap["errors"] = list(self._job["errors"])
        snap["remaining"] = len(self._pending)
        return snap

    def status(self) -> dict[str, Any]:
        with self._cond:
            return self._snapshot_locked()

    def start(self, round_ids: list[int], *, force: bool = False) -> dict[str, Any]:
        deduped: list[int] = []
        for raw_id in round_ids:
            try:
                rid = int(raw_id)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"无效的 round_id: {raw_id}") from exc
            if rid <= 0:
                raise ValueError(f"无效的 round_id: {raw_id}")
            if rid not in deduped:
                deduped.append(rid)

        if not deduped:
            raise ValueError("请至少选择 1 条标的记录")
        if len(deduped) > MAX_BATCH_ENRICH_SIZE:
            raise ValueError(f"单次最多批量采集 {MAX_BATCH_ENRICH_SIZE} 条（当前选择 {len(deduped)} 条）")

        with AuctionDB(self._config.storage.db_path) as db:
            placeholders = ",".join("?" for _ in deduped)
            rows = db.conn.execute(
                f"""
                SELECT id AS round_id, platform, item_id, url, title, detail_fetched_at
                FROM auction_rounds
                WHERE id IN ({placeholders})
                """,
                deduped,
            ).fetchall()

        by_id = {int(r["round_id"]): dict(r) for r in rows}
        pending: list[dict[str, Any]] = []
        skipped = 0
        for rid in deduped:
            rec = by_id.get(rid)
            if rec is None:
                skipped += 1
                continue
            if not force and rec.get("detail_fetched_at"):
                skipped += 1
                continue
            pending.append(rec)

        with self._browser_lock:
            with self._cond:
                if self._job["status"] in {"running", "auth_required"}:
                    raise JobConflictError("已有进行中或等待验证的采集任务，请先完成或终止后再发起新任务")
                if self._is_console_busy_cb and self._is_console_busy_cb():
                    raise JobConflictError("「任务控制台」正在运行浏览器采集任务，请先等待完成或停止后再发起批量采集")

                now = now_str()
                job_id = uuid.uuid4().hex[:10]
                self._pending = pending
                self._job = {
                    "job_id": job_id,
                    "status": "running" if pending else "completed",
                    "total": len(deduped),
                    "processed": skipped,
                    "succeeded": 0,
                    "skipped": skipped,
                    "failed": 0,
                    "force": bool(force),
                    "current_round_id": None,
                    "current_item_id": "",
                    "current_title": "",
                    "auth_message": "",
                    "auth_url": "",
                    "task_space": self._config.provider.task_space,
                    "started_at": now,
                    "finished_at": "" if pending else now,
                    "errors": [],
                }
                if pending:
                    worker = threading.Thread(target=self._run_job, args=(job_id,), daemon=True)
                    worker.start()
                return self._snapshot_locked()

    def resume(self) -> dict[str, Any]:
        with self._cond:
            if self._job["status"] != "auth_required":
                raise ValueError("当前没有处于等待验证状态的采集任务")
            self._job["status"] = "running"
            self._job["auth_message"] = ""
            self._job["auth_url"] = ""
            self._cond.notify_all()
            return self._snapshot_locked()

    def cancel(self) -> dict[str, Any]:
        with self._cond:
            if self._job["status"] in {"running", "auth_required"}:
                self._job["status"] = "cancelled"
                self._job["auth_message"] = ""
                self._job["auth_url"] = ""
                self._job["current_round_id"] = None
                self._job["current_item_id"] = ""
                self._job["current_title"] = ""
                self._job["finished_at"] = now_str()
                self._pending.clear()
                self._cond.notify_all()
            return self._snapshot_locked()

    def _run_job(self, job_id: str) -> None:
        try:
            providers = self._provider_factory(self._config)
        except Exception as exc:
            logger.exception("初始化采集驱动失败")
            with self._cond:
                if self._job["job_id"] == job_id:
                    self._job["status"] = "error"
                    self._job["errors"].append(f"初始化采集驱动失败: {exc}")
                    self._job["finished_at"] = now_str()
                    self._pending.clear()
            return

        requests_made = 0
        while True:
            with self._cond:
                if self._job["job_id"] != job_id or self._job["status"] == "cancelled":
                    return
                if not self._pending:
                    self._job["status"] = "completed"
                    self._job["current_round_id"] = None
                    self._job["current_item_id"] = ""
                    self._job["current_title"] = ""
                    self._job["finished_at"] = now_str()
                    return
                item = self._pending[0]
                self._job["current_round_id"] = item["round_id"]
                self._job["current_item_id"] = item["item_id"]
                self._job["current_title"] = item["title"]

            if requests_made > 0:
                lo = max(0.0, self._config.provider.min_delay_seconds)
                hi = max(lo, self._config.provider.max_delay_seconds)
                if hi > 0:
                    self._sleep(random.uniform(lo, hi))
                with self._cond:
                    if self._job["job_id"] != job_id or self._job["status"] == "cancelled":
                        return

            platform_name = str(item["platform"])
            prov = providers.get(platform_name)
            if prov is None:
                for p in providers.values():
                    if p.platform == platform_name:
                        prov = p
                        break
            if prov is None:
                try:
                    prov = build_provider(self._config, platform_name)
                    providers[platform_name] = prov
                except Exception as exc:
                    with self._cond:
                        if self._job["job_id"] == job_id and self._pending and self._pending[0]["round_id"] == item["round_id"]:
                            self._pending.pop(0)
                            self._job["failed"] += 1
                            self._job["processed"] += 1
                            if len(self._job["errors"]) < 20:
                                self._job["errors"].append(f"{item['item_id']}: {exc}")
                    continue

            requests_made += 1
            try:
                detail = prov.fetch_detail(item["item_id"], item["url"])
                with AuctionDB(self._config.storage.db_path) as db:
                    db.apply_detail(detail)
                with self._cond:
                    if self._job["job_id"] == job_id and self._pending and self._pending[0]["round_id"] == item["round_id"]:
                        self._pending.pop(0)
                        self._job["succeeded"] += 1
                        self._job["processed"] += 1
            except AuthRequiredError as exc:
                with self._cond:
                    if self._job["job_id"] != job_id or self._job["status"] == "cancelled":
                        return
                    self._job["status"] = "auth_required"
                    self._job["auth_message"] = str(exc)
                    self._job["auth_url"] = exc.url
                    self._job["task_space"] = exc.task_space or self._config.provider.task_space
                    while self._job["job_id"] == job_id and self._job["status"] == "auth_required":
                        self._cond.wait(timeout=1.0)
                    if self._job["job_id"] != job_id or self._job["status"] == "cancelled":
                        return
            except ProviderError as exc:
                logger.warning("Web 批量补抓详情失败 platform=%s item_id=%s: %s", platform_name, item["item_id"], exc)
                try:
                    with AuctionDB(self._config.storage.db_path) as db:
                        db.record_detail_attempt(platform_name, item["item_id"])
                except Exception:
                    pass
                with self._cond:
                    if self._job["job_id"] == job_id and self._pending and self._pending[0]["round_id"] == item["round_id"]:
                        self._pending.pop(0)
                        self._job["failed"] += 1
                        self._job["processed"] += 1
                        if len(self._job["errors"]) < 20:
                            self._job["errors"].append(f"{item['item_id']}: {exc}")
            except Exception as exc:
                logger.exception("Web 批量补抓详情发生未预期异常 platform=%s item_id=%s", platform_name, item["item_id"])
                with self._cond:
                    if self._job["job_id"] == job_id and self._pending and self._pending[0]["round_id"] == item["round_id"]:
                        self._pending.pop(0)
                        self._job["failed"] += 1
                        self._job["processed"] += 1
                        if len(self._job["errors"]) < 20:
                            self._job["errors"].append(f"{item['item_id']}: {exc}")


def _default_process_factory(cmd: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )


class CliJobManager:
    """Single-worker subprocess manager for running allowlisted fph CLI actions from the Web Console."""

    def __init__(
        self,
        config: AppConfig,
        *,
        process_factory: Callable[[list[str], dict[str, str]], Any] | None = None,
        is_browser_busy_cb: Callable[[], bool] | None = None,
        browser_lock: threading.Lock | None = None,
    ) -> None:
        self._config = config
        self._process_factory = process_factory or _default_process_factory
        self._is_browser_busy_cb = is_browser_busy_cb
        self._browser_lock = browser_lock or threading.Lock()
        self._lock = threading.Lock()
        self._logs: deque[dict[str, Any]] = deque(maxlen=1000)
        self._next_seq = 1
        self._proc: Any = None
        self._stop_requested = False
        self._last_spec: tuple[str, dict[str, Any]] | None = None
        self._job: dict[str, Any] = {
            "job_id": "",
            "action": "",
            "status": "idle",  # idle / running / auth_required / succeeded / failed / stopped
            "command_preview": "",
            "exit_code": None,
            "started_at": "",
            "finished_at": "",
            "needs_auth": False,
        }

    def is_browser_busy(self) -> bool:
        with self._lock:
            return self._job["status"] in {"running", "auth_required"} and self._job["action"] in _BROWSER_CONSOLE_ACTIONS

    def _parse_optional_int(self, params: dict[str, Any], key: str, *, minimum: int, maximum: int | None = None) -> int | None:
        raw = params.get(key)
        if raw is None or raw == "":
            return None
        if isinstance(raw, bool):
            raise ValueError(f"参数 {key} 必须是整数")
        try:
            val = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"参数 {key} 必须是整数") from exc
        if val < minimum:
            raise ValueError(f"参数 {key} 必须 >= {minimum}")
        if maximum is not None and val > maximum:
            raise ValueError(f"参数 {key} 必须 <= {maximum}")
        return val

    def build_command(self, action: str, params: dict[str, Any] | None = None) -> tuple[list[str], str]:
        act = (action or "").strip().lower()
        if act not in _ALLOWED_CONSOLE_ACTIONS:
            raise ValueError(f"不支持的控制动作: {action}（允许: {', '.join(sorted(_ALLOWED_CONSOLE_ACTIONS))}）")
        p = params or {}
        if not isinstance(p, dict):
            raise ValueError("params 必须是 JSON 对象")

        cmd: list[str] = [sys.executable, "-u", "-m", "fph.cli"]
        if self._config.config_path is not None and self._config.config_path.exists():
            cmd.extend(["-c", str(self._config.config_path)])

        if act == "doctor":
            cmd.append("doctor")
        elif act == "sync":
            cmd.extend(["sync", "--no-wait-auth"])
            region = str(p.get("region") or "").strip()
            if region:
                cmd.extend(["--region", region])
            provider = str(p.get("provider") or "").strip().lower()
            if provider and provider != "all":
                if provider not in _ALLOWED_CLI_PROVIDERS:
                    raise ValueError(f"不支持的采集平台: {provider}")
                cmd.extend(["--provider", provider])
            max_pages = self._parse_optional_int(p, "max_pages", minimum=1, maximum=200)
            if max_pages is not None:
                cmd.extend(["--max-pages", str(max_pages)])
            lookback_days = self._parse_optional_int(p, "lookback_days", minimum=0, maximum=3650)
            if lookback_days is not None:
                cmd.extend(["--lookback-days", str(lookback_days)])
            if bool(p.get("active_only", False)):
                cmd.append("--active-only")
            if bool(p.get("no_enrich", False)):
                cmd.append("--no-enrich")
            if bool(p.get("no_refresh_ended", False)):
                cmd.append("--no-refresh-ended")
            if bool(p.get("reset_checkpoint", False)):
                cmd.append("--reset-checkpoint")
        elif act == "enrich":
            cmd.extend(["enrich", "--no-wait-auth"])
            provider = str(p.get("provider") or "").strip().lower()
            if provider and provider != "all":
                if provider not in _ALLOWED_CLI_PROVIDERS:
                    raise ValueError(f"不支持的采集平台: {provider}")
                cmd.extend(["--provider", provider])
            limit = self._parse_optional_int(p, "limit", minimum=1, maximum=200)
            if limit is not None:
                cmd.extend(["--limit", str(limit)])
            statuses = str(p.get("statuses") or "").strip()
            if statuses:
                cmd.extend(["--statuses", statuses])
            lookback_days = self._parse_optional_int(p, "lookback_days", minimum=0, maximum=3650)
            if lookback_days is not None:
                cmd.extend(["--lookback-days", str(lookback_days)])
        elif act == "cleanup":
            cmd.append("cleanup")
            if bool(p.get("dry_run", True)):
                cmd.append("--dry-run")
            lookback_days = self._parse_optional_int(p, "lookback_days", minimum=0, maximum=3650)
            if lookback_days is not None:
                cmd.extend(["--lookback-days", str(lookback_days)])
            if bool(p.get("keep_non_residential", False)):
                cmd.append("--keep-non-residential")
            if bool(p.get("no_reset_captcha", False)):
                cmd.append("--no-reset-captcha")
        elif act == "schedule":
            cmd.append("schedule")
            interval = self._parse_optional_int(p, "interval", minimum=5, maximum=10080)
            if interval is not None:
                cmd.extend(["--interval", str(interval)])
            if bool(p.get("delay_first", False)):
                cmd.append("--delay-first")

        preview_idx = cmd.index("fph.cli") + 1
        preview = "fph " + " ".join(cmd[preview_idx:])
        return cmd, preview

    def _append_log_locked(self, text: str) -> None:
        self._logs.append({"seq": self._next_seq, "text": text})
        self._next_seq += 1

    def _snapshot_locked(self, since: int = 0) -> dict[str, Any]:
        lines = [item for item in self._logs if item["seq"] > since]
        snap = dict(self._job)
        snap["lines"] = lines
        snap["next_cursor"] = self._next_seq - 1
        return snap

    def status(self, since: int = 0) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked(since=since)

    def start(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        cmd, preview = self.build_command(action, params)
        act = (action or "").strip().lower()

        with self._browser_lock:
            with self._lock:
                if self._job["status"] == "running":
                    raise JobConflictError("控制台已有任务正在运行，请先等待完成或停止后再发起新任务")
                if act in _BROWSER_CONSOLE_ACTIONS and self._is_browser_busy_cb and self._is_browser_busy_cb():
                    raise JobConflictError("「批量勾选详情」正在使用浏览器，请先等待完成或终止后再执行")

                env = os.environ.copy()
                env["PYTHONUNBUFFERED"] = "1"
                proc = self._process_factory(cmd, env)

                job_id = uuid.uuid4().hex[:10]
                self._proc = proc
                self._stop_requested = False
                self._logs.clear()
                self._next_seq = 1
                if act in {"sync", "enrich"}:
                    self._last_spec = (act, dict(params or {}))
                self._job = {
                    "job_id": job_id,
                    "action": act,
                    "status": "running",
                    "command_preview": preview,
                    "exit_code": None,
                    "started_at": now_str(),
                    "finished_at": "",
                    "needs_auth": False,
                }
                self._append_log_locked(f"$ {preview}")
                reader = threading.Thread(target=self._read_loop, args=(job_id, proc), daemon=True)
                reader.start()
                return self._snapshot_locked(since=0)

    def _read_loop(self, job_id: str, proc: Any) -> None:
        saw_auth_hint = False
        try:
            stdout = getattr(proc, "stdout", None)
            if stdout is not None:
                for raw_line in iter(stdout.readline, ""):
                    line = raw_line.rstrip("\r\n")
                    if any(hint in line for hint in _AUTH_LOG_HINTS):
                        saw_auth_hint = True
                    with self._lock:
                        if self._job["job_id"] != job_id:
                            return
                        self._append_log_locked(line)
        except Exception as exc:
            with self._lock:
                if self._job["job_id"] == job_id:
                    self._append_log_locked(f"[读取日志异常] {exc}")
        finally:
            try:
                exit_code = proc.wait(timeout=5)
            except Exception:
                exit_code = -1

            with self._lock:
                if self._job["job_id"] == job_id:
                    self._proc = None
                    self._job["exit_code"] = exit_code
                    self._job["finished_at"] = now_str()
                    if self._stop_requested:
                        self._job["status"] = "stopped"
                        self._append_log_locked("[任务已停止]")
                    elif exit_code == 0:
                        self._job["status"] = "succeeded"
                        self._append_log_locked("[任务执行完成，退出码 0]")
                    elif exit_code == 2 and saw_auth_hint:
                        self._job["status"] = "auth_required"
                        self._job["needs_auth"] = True
                        self._append_log_locked("[需要人机验证] 请在 ego(lite) 完成滑块或登录后，点击「已完成验证，继续执行」")
                    else:
                        self._job["status"] = "failed"
                        self._append_log_locked(f"[任务结束，退出码 {exit_code}]")

    def stop(self) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
            if proc is None or self._job["status"] != "running":
                return self._snapshot_locked(since=0)
            self._stop_requested = True
            self._append_log_locked("[正在停止任务...]")
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        with self._lock:
            return self._snapshot_locked(since=0)

    def retry(self) -> dict[str, Any]:
        with self._lock:
            spec = self._last_spec
        if spec is None:
            raise ValueError("没有可继续执行的历史任务")
        action, params = spec
        retry_params = dict(params)
        retry_params["reset_checkpoint"] = False
        return self.start(action, retry_params)


class _FphHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        config: AppConfig,
        *,
        provider_factory: Callable[[AppConfig], dict[str, AuctionProvider]] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        process_factory: Callable[[list[str], dict[str, str]], Any] | None = None,
    ) -> None:
        self.app_config = config
        self._browser_lock = threading.Lock()
        self.cli_manager = CliJobManager(
            config,
            process_factory=process_factory,
            browser_lock=self._browser_lock,
        )
        self.enrich_manager = EnrichJobManager(
            config,
            provider_factory=provider_factory,
            sleep_fn=sleep_fn,
            is_console_busy_cb=self.cli_manager.is_browser_busy,
            browser_lock=self._browser_lock,
        )
        self.cli_manager._is_browser_busy_cb = lambda: (
            self.enrich_manager.status().get("status") in {"running", "auth_required"}
        )
        super().__init__(server_address, handler_cls)


class _FphRequestHandler(BaseHTTPRequestHandler):
    server: _FphHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("web %s - %s", self.address_string(), format % args)

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        parsed = urlsplit(self.path)
        path = parsed.path
        params = parse_qs(parsed.query, keep_blank_values=False)

        try:
            if path in {"/", "/index.html"}:
                if not _UI_HTML_PATH.exists():
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "缺少前端页面文件 web_ui.html"})
                    return
                html_bytes = _UI_HTML_PATH.read_bytes()
                self._send_bytes(HTTPStatus.OK, html_bytes, "text/html; charset=utf-8")
                return

            if path == "/api/overview":
                with AuctionDB(self.server.app_config.storage.db_path) as db:
                    data = _query_overview(db, self.server.app_config.storage.db_path)
                self._send_json(HTTPStatus.OK, data)
                return

            if path == "/api/items":
                with AuctionDB(self.server.app_config.storage.db_path) as db:
                    data = _query_items(db, params)
                self._send_json(HTTPStatus.OK, data)
                return

            if path.startswith("/api/items/"):
                raw_id = path[len("/api/items/") :].strip("/")
                try:
                    round_id = int(raw_id)
                except ValueError:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"无效的 round_id: {raw_id}"})
                    return
                with AuctionDB(self.server.app_config.storage.db_path) as db:
                    detail = _query_item_detail(db, round_id)
                if detail is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": f"未找到拍卖轮次 round_id={round_id}"})
                    return
                self._send_json(HTTPStatus.OK, detail)
                return

            if path == "/api/stats":
                city = _first_param(params, "city") or None
                by_district = _parse_bool(_first_param(params, "by_district"))
                by_month = _parse_bool(_first_param(params, "by_month"))
                with AuctionDB(self.server.app_config.storage.db_path) as db:
                    rows = get_market_summary(db, city=city, by_district=by_district, by_month=by_month)
                self._send_json(HTTPStatus.OK, {"rows": rows})
                return

            if path == "/api/bargains":
                city = _first_param(params, "city") or None
                district = _first_param(params, "district") or None
                max_discount = _parse_optional_float(_first_param(params, "max_discount"))
                if max_discount is None:
                    max_discount = 0.80
                max_total = _parse_optional_float(_first_param(params, "max_total"))
                min_area = _parse_optional_float(_first_param(params, "min_area"))
                max_risk = _first_param(params, "max_risk", "medium")
                if max_risk not in {"low", "medium", "high"}:
                    raise ValueError(f"无效的 max_risk: {max_risk}（可选: low / medium / high）")
                stage = _first_param(params, "stage") or None
                exclude_stage = _first_param(params, "exclude_stage") or None
                limit = _parse_int(_first_param(params, "limit"), 50, minimum=1, maximum=200)
                with AuctionDB(self.server.app_config.storage.db_path) as db:
                    rows = find_bargain_candidates(
                        db,
                        city=city,
                        district=district,
                        max_discount=max_discount,
                        max_total_wan=max_total,
                        min_area=min_area,
                        max_risk_level=max_risk,
                        stage=stage,
                        exclude_stage=exclude_stage,
                        limit=limit,
                    )
                self._send_json(HTTPStatus.OK, {"rows": rows})
                return

            if path == "/api/sync-runs":
                with AuctionDB(self.server.app_config.storage.db_path) as db:
                    rows = _query_sync_runs(db)
                self._send_json(HTTPStatus.OK, {"rows": rows})
                return

            if path == "/api/enrich/status":
                self._send_json(HTTPStatus.OK, self.server.enrich_manager.status())
                return

            if path == "/api/console/options":
                cfg = self.server.app_config
                regions = [
                    {
                        "name": r.name,
                        "city": r.city,
                        "provider": resolve_region_provider_name(cfg, r),
                    }
                    for r in cfg.regions
                ]
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "regions": regions,
                        "defaults": {
                            "lookback_days": cfg.provider.lookback_days,
                            "max_pages_per_query": cfg.provider.max_pages_per_query,
                            "max_detail_enrich_per_run": cfg.provider.max_detail_enrich_per_run,
                            "schedule_interval_minutes": cfg.schedule.interval_minutes,
                        },
                    },
                )
                return

            if path == "/api/console/status":
                since = _parse_int(_first_param(params, "since"), 0, minimum=0)
                self._send_json(HTTPStatus.OK, self.server.cli_manager.status(since=since))
                return

            if path == "/api/export":
                dataset = _first_param(params, "dataset", "all")
                city = _first_param(params, "city") or None
                fmt = _first_param(params, "format", "csv").lower()
                with AuctionDB(self.server.app_config.storage.db_path) as db:
                    body, content_type, filename = _build_export_payload(
                        db,
                        dataset=dataset,
                        city=city,
                        fmt=fmt,
                    )
                self._send_bytes(
                    HTTPStatus.OK,
                    body,
                    content_type,
                    extra_headers={"Content-Disposition": f'attachment; filename="{filename}"'},
                )
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"未知路径: {path}"})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.exception("处理请求失败 path=%s", path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"服务端内部错误: {exc}"})

    def _read_json_body(self) -> dict[str, Any]:
        raw_len = self.headers.get("Content-Length", "0").strip()
        try:
            length = int(raw_len)
        except ValueError as exc:
            raise ValueError("无效的 Content-Length") from exc
        if length <= 0:
            return {}
        if length > 1024 * 1024:
            raise ValueError("请求体过大")
        raw_bytes = self.rfile.read(length)
        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"无效的 JSON 请求体: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON 请求体必须是对象")
        return data

    def _validate_post_request(self) -> None:
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise PermissionError("POST 请求必须使用 Content-Type: application/json")
        host = (self.headers.get("Host") or "").strip().lower()
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            if origin == "null" or urlsplit(origin).netloc.lower() != host:
                raise PermissionError("拒绝跨站 Origin 请求")
            return
        referer = (self.headers.get("Referer") or "").strip()
        if referer and urlsplit(referer).netloc.lower() != host:
            raise PermissionError("拒绝跨站 Referer 请求")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        path = urlsplit(self.path).path
        try:
            self._validate_post_request()
            if path == "/api/enrich":
                payload = self._read_json_body()
                round_ids = payload.get("round_ids")
                if not isinstance(round_ids, list):
                    raise ValueError("round_ids 必须是整数数组")
                force = bool(payload.get("force", False))
                snap = self.server.enrich_manager.start(round_ids, force=force)
                self._send_json(HTTPStatus.ACCEPTED, snap)
                return

            if path == "/api/enrich/resume":
                snap = self.server.enrich_manager.resume()
                self._send_json(HTTPStatus.OK, snap)
                return

            if path == "/api/enrich/cancel":
                snap = self.server.enrich_manager.cancel()
                self._send_json(HTTPStatus.OK, snap)
                return

            if path == "/api/console/run":
                payload = self._read_json_body()
                action = str(payload.get("action") or "")
                run_params = payload.get("params") if "params" in payload else {}
                snap = self.server.cli_manager.start(action, run_params)
                self._send_json(HTTPStatus.ACCEPTED, snap)
                return

            if path == "/api/console/stop":
                snap = self.server.cli_manager.stop()
                self._send_json(HTTPStatus.OK, snap)
                return

            if path == "/api/console/retry":
                snap = self.server.cli_manager.retry()
                self._send_json(HTTPStatus.ACCEPTED, snap)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"未知路径: {path}"})
        except PermissionError as exc:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
        except JobConflictError as exc:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.exception("处理 POST 请求失败 path=%s", path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"服务端内部错误: {exc}"})


def create_web_server(
    config: AppConfig,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    provider_factory: Callable[[AppConfig], dict[str, AuctionProvider]] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    process_factory: Callable[[list[str], dict[str, str]], Any] | None = None,
) -> ThreadingHTTPServer:
    """Create a threaded local HTTP server serving the FPH data dashboard."""
    return _FphHTTPServer(
        (host, port),
        _FphRequestHandler,
        config,
        provider_factory=provider_factory,
        sleep_fn=sleep_fn,
        process_factory=process_factory,
    )


def run_web_server(
    config: AppConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    """Start the local FPH web dashboard until interrupted with Ctrl+C."""
    try:
        server = create_web_server(config, host=host, port=port)
    except OSError as exc:
        raise RuntimeError(
            f"无法在 {host}:{port} 启动 Web 服务（端口可能已被占用）: {exc}。可尝试使用 `--port` 指定其他端口。"
        ) from exc

    actual_host, actual_port = server.server_address[:2]
    display_host = "127.0.0.1" if actual_host in {"0.0.0.0", "::", ""} else actual_host
    url = f"http://{display_host}:{actual_port}"

    print(f"FPH 本地数据看板已启动: {url}")
    print(f"数据库路径: {config.storage.db_path.resolve()}")
    print("按 Ctrl+C 退出服务。")

    if open_browser:
        timer = threading.Timer(0.6, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断信号，本地数据看板已关闭。")
    finally:
        server.cli_manager.stop()
        server.enrich_manager.cancel()
        server.server_close()
