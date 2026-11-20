from __future__ import annotations

import csv
import io
import json
import logging
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fph.analytics import find_bargain_candidates, get_market_summary
from fph.config import AppConfig
from fph.db import AuctionDB

logger = logging.getLogger(__name__)

_UI_HTML_PATH = Path(__file__).with_name("web_ui.html")

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
        like_q = f"%{q}%"
        where_clauses.append(
            "(title LIKE ? OR address LIKE ? OR community_name LIKE ? OR court_name LIKE ? OR item_id LIKE ?)"
        )
        sql_params.extend([like_q, like_q, like_q, like_q, like_q])
    if platform and platform != "all":
        where_clauses.append("platform = ?")
        sql_params.append(platform)
    if city:
        where_clauses.append("city LIKE ?")
        sql_params.append(f"%{city.replace('市', '')}%")
    if district:
        where_clauses.append("district LIKE ?")
        sql_params.append(f"%{district}%")
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
        where_clauses.append("stage = ?")
        sql_params.append(stage)
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
        where_sql = "WHERE city LIKE ?"
        params.append(f"%{city.replace('市', '')}%")

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


class _FphHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_cls: type[BaseHTTPRequestHandler], config: AppConfig) -> None:
        self.app_config = config
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
                        limit=limit,
                    )
                self._send_json(HTTPStatus.OK, {"rows": rows})
                return

            if path == "/api/sync-runs":
                with AuctionDB(self.server.app_config.storage.db_path) as db:
                    rows = _query_sync_runs(db)
                self._send_json(HTTPStatus.OK, {"rows": rows})
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


def create_web_server(
    config: AppConfig,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    """Create a threaded local HTTP server serving the FPH data dashboard."""
    return _FphHTTPServer((host, port), _FphRequestHandler, config)


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
        server.server_close()
