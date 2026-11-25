from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from fph.models import RawAuctionDetail, RawAuctionItem, SyncSummary
from fph.normalizer import (
    classify_property_type,
    extract_area_sqm,
    normalize_datetime,
    normalize_stage,
    normalize_status,
    parse_property_info,
)

CN_TZ = timezone(timedelta(hours=8))

_AUCTION_ROUNDS_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("detail_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("detail_last_attempt_at", "TEXT"),
    ("refresh_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("refresh_last_attempt_at", "TEXT"),
)

_CAPTCHA_NOTICE_MARKERS: tuple[str, ...] = (
    "拖动下方滑块",
    "请按住滑块",
    "通过验证以确保正常访问",
)

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS properties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_key TEXT NOT NULL UNIQUE,
    province TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    district TEXT NOT NULL DEFAULT '',
    community_name TEXT NOT NULL DEFAULT '',
    normalized_address TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    property_type TEXT NOT NULL DEFAULT 'other',
    is_partial_share INTEGER NOT NULL DEFAULT 0,
    ownership_share TEXT NOT NULL DEFAULT '100%',
    area_sqm REAL,
    risk_tags_json TEXT NOT NULL DEFAULT '[]',
    risk_level TEXT NOT NULL DEFAULT 'low',
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_properties_city_district
    ON properties (city, district, property_type);

CREATE TABLE IF NOT EXISTS auction_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    item_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '未知',
    status TEXT NOT NULL DEFAULT 'unknown',
    initial_price REAL,
    current_price REAL,
    consult_price REAL,
    deposit_price REAL,
    discount_rate REAL,
    unit_price REAL,
    bid_count INTEGER,
    apply_count INTEGER,
    watch_count INTEGER,
    start_time TEXT,
    end_time TEXT,
    court_name TEXT NOT NULL DEFAULT '',
    notice_text TEXT NOT NULL DEFAULT '',
    survey_text TEXT NOT NULL DEFAULT '',
    detail_fetched_at TEXT,
    detail_attempts INTEGER NOT NULL DEFAULT 0,
    detail_last_attempt_at TEXT,
    refresh_attempts INTEGER NOT NULL DEFAULT 0,
    refresh_last_attempt_at TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(platform, item_id)
);

CREATE INDEX IF NOT EXISTS idx_rounds_property ON auction_rounds (property_id);
CREATE INDEX IF NOT EXISTS idx_rounds_status_end ON auction_rounds (status, end_time);
CREATE INDEX IF NOT EXISTS idx_rounds_detail_fetched ON auction_rounds (detail_fetched_at);

CREATE TABLE IF NOT EXISTS auction_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL REFERENCES auction_rounds(id) ON DELETE CASCADE,
    captured_at TEXT NOT NULL,
    status TEXT NOT NULL,
    current_price REAL,
    bid_count INTEGER,
    apply_count INTEGER,
    watch_count INTEGER
);

CREATE INDEX IF NOT EXISTS idx_snapshots_round_time
    ON auction_snapshots (round_id, captured_at);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    region_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    items_seen INTEGER NOT NULL DEFAULT 0,
    properties_created INTEGER NOT NULL DEFAULT 0,
    rounds_upserted INTEGER NOT NULL DEFAULT 0,
    details_enriched INTEGER NOT NULL DEFAULT 0,
    ended_refreshed INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sync_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    region_name TEXT NOT NULL,
    scope TEXT NOT NULL,
    next_page INTEGER NOT NULL DEFAULT 1,
    backfill_completed INTEGER NOT NULL DEFAULT 0,
    last_status TEXT NOT NULL DEFAULT 'pending',
    updated_at TEXT NOT NULL,
    UNIQUE(provider, region_name, scope)
);

CREATE VIEW IF NOT EXISTS v_auction_full AS
SELECT
    r.id AS round_id,
    r.property_id,
    p.property_key,
    r.platform,
    r.item_id,
    r.url,
    r.title,
    p.province,
    p.city,
    p.district,
    p.community_name,
    p.address,
    p.property_type,
    p.is_partial_share,
    p.ownership_share,
    p.area_sqm,
    p.risk_tags_json,
    p.risk_level,
    r.stage,
    r.status,
    r.initial_price,
    r.current_price,
    r.consult_price,
    r.deposit_price,
    CASE
        WHEN r.consult_price IS NOT NULL AND r.consult_price > 0
             AND COALESCE(r.current_price, r.initial_price) IS NOT NULL
        THEN ROUND(COALESCE(r.current_price, r.initial_price) / r.consult_price, 4)
        ELSE r.discount_rate
    END AS discount_rate,
    CASE
        WHEN p.area_sqm IS NOT NULL AND p.area_sqm > 0
             AND COALESCE(r.current_price, r.initial_price) IS NOT NULL
        THEN ROUND(COALESCE(r.current_price, r.initial_price) / p.area_sqm, 2)
        ELSE r.unit_price
    END AS unit_price,
    r.bid_count,
    r.apply_count,
    r.watch_count,
    r.start_time,
    r.end_time,
    r.court_name,
    r.detail_fetched_at,
    r.last_seen_at,
    (SELECT COUNT(*) FROM auction_rounds r2 WHERE r2.property_id = r.property_id) AS total_rounds_for_property
FROM auction_rounds r
JOIN properties p ON p.id = r.property_id;

CREATE VIEW IF NOT EXISTS v_residential_deals AS
SELECT *
FROM v_auction_full
WHERE property_type = 'residential'
  AND is_partial_share = 0
  AND status IN ('sold', 'failed', 'ended');

CREATE VIEW IF NOT EXISTS v_bargain_candidates AS
SELECT *
FROM v_auction_full
WHERE property_type = 'residential'
  AND is_partial_share = 0
  AND status IN ('upcoming', 'ongoing');
"""


def now_str() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _calc_discount(price: float | None, consult: float | None) -> float | None:
    if price is None or consult is None or consult <= 0:
        return None
    return round(price / consult, 4)


def _calc_unit_price(price: float | None, area: float | None) -> float | None:
    if price is None or area is None or area <= 0:
        return None
    return round(price / area, 2)


def _has_captcha_notice(text: str | None) -> bool:
    if not text:
        return False
    return any(marker in text for marker in _CAPTCHA_NOTICE_MARKERS)


def _iter_chunks(items: Sequence[int], chunk_size: int = 500) -> list[list[int]]:
    seq = list(items)
    return [seq[i : i + chunk_size] for i in range(0, len(seq), chunk_size)]


def escape_like(value: str) -> str:
    """Escape SQLite LIKE wildcards (\\, %, _) for use with ESCAPE '\\'."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_district_filter(district: str | None) -> tuple[str, list[str]] | None:
    """Build a SQL WHERE clause and params supporting single or comma-separated districts."""
    if not district:
        return None
    vals = [part.strip() for part in district.split(",") if part.strip()]
    if not vals:
        return None
    if len(vals) == 1:
        return "district LIKE ? ESCAPE '\\'", [f"%{escape_like(vals[0])}%"]
    clauses = " OR ".join("district LIKE ? ESCAPE '\\'" for _ in vals)
    return f"({clauses})", [f"%{escape_like(v)}%" for v in vals]


class AuctionDB:
    def __init__(self, db_path: str | Path, *, read_only: bool = False) -> None:
        self.db_path = Path(db_path)
        self.read_only = read_only
        if read_only:
            if not self.db_path.exists():
                raise FileNotFoundError(f"数据库文件不存在: {self.db_path}")
            self.conn = sqlite3.connect(
                f"{self.db_path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=30.0,
            )
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout = 30000")
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        existing_cols = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(auction_rounds)").fetchall()
        }
        for col_name, col_def in _AUCTION_ROUNDS_MIGRATIONS:
            if col_name not in existing_cols:
                self.conn.execute(
                    f"ALTER TABLE auction_rounds ADD COLUMN {col_name} {col_def}"
                )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> AuctionDB:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def start_sync_run(self, provider: str, region_name: str) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO sync_runs (provider, region_name, started_at, status)
            VALUES (?, ?, ?, 'running')
            """,
            (provider, region_name, now_str()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_sync_run(self, summary: SyncSummary) -> None:
        self.conn.execute(
            """
            UPDATE sync_runs
            SET finished_at = ?,
                status = ?,
                pages_fetched = ?,
                items_seen = ?,
                properties_created = ?,
                rounds_upserted = ?,
                details_enriched = ?,
                ended_refreshed = ?,
                message = ?
            WHERE id = ?
            """,
            (
                now_str(),
                summary.status,
                summary.pages_fetched,
                summary.items_seen,
                summary.properties_created,
                summary.rounds_upserted,
                summary.details_enriched,
                summary.ended_refreshed,
                summary.message,
                summary.run_id,
            ),
        )
        self.conn.commit()

    def upsert_list_item(self, item: RawAuctionItem) -> tuple[int, int, bool]:
        """Upsert property + auction_round + conditional snapshot.

        Returns (property_id, round_id, property_created).
        """
        try:
            return self._upsert_list_item_tx(item)
        except Exception:
            self.conn.rollback()
            raise

    def _upsert_list_item_tx(self, item: RawAuctionItem) -> tuple[int, int, bool]:
        ts = now_str()
        parsed = parse_property_info(
            item_id=item.item_id,
            title=item.title,
            province=item.province,
            city=item.city,
            explicit_district=item.district,
            address=item.address,
            category_hint=item.category_hint,
            purpose_hint=item.purpose_hint,
            explicit_area=item.area_sqm,
        )

        effective_community = item.community_name or parsed.community_name
        effective_address = item.address or item.title
        effective_area = item.area_sqm if item.area_sqm is not None else parsed.area_sqm

        # 1. Upsert property
        row = self.conn.execute(
            "SELECT * FROM properties WHERE property_key = ?",
            (parsed.property_key,),
        ).fetchone()

        property_created = False
        if row is None:
            cur = self.conn.execute(
                """
                INSERT INTO properties (
                    property_key, province, city, district, community_name,
                    normalized_address, address, property_type, is_partial_share,
                    ownership_share, area_sqm, risk_tags_json, risk_level,
                    first_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    parsed.property_key,
                    item.province,
                    item.city,
                    parsed.district,
                    effective_community,
                    parsed.normalized_address,
                    effective_address,
                    parsed.property_type,
                    int(parsed.is_partial_share),
                    parsed.ownership_share,
                    effective_area,
                    json.dumps(parsed.risk_tags, ensure_ascii=False),
                    parsed.risk_level,
                    ts,
                    ts,
                ),
            )
            property_id = int(cur.lastrowid)
            area_sqm = effective_area
            property_created = True
        else:
            property_id = int(row["id"])
            area_sqm = row["area_sqm"] if row["area_sqm"] is not None else effective_area
            district = row["district"] or parsed.district
            community = row["community_name"] or effective_community
            prop_type = row["property_type"] if row["property_type"] != "other" else parsed.property_type
            existing_tags = json.loads(row["risk_tags_json"] or "[]")
            merged_tags = list(dict.fromkeys(existing_tags + parsed.risk_tags))
            risk_rank = {"low": 0, "medium": 1, "high": 2}
            merged_risk_level = (
                row["risk_level"]
                if risk_rank.get(row["risk_level"], 0) >= risk_rank.get(parsed.risk_level, 0)
                else parsed.risk_level
            )

            self.conn.execute(
                """
                UPDATE properties
                SET district = ?,
                    community_name = ?,
                    property_type = ?,
                    is_partial_share = MAX(is_partial_share, ?),
                    area_sqm = ?,
                    risk_tags_json = ?,
                    risk_level = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    district,
                    community,
                    prop_type,
                    int(parsed.is_partial_share),
                    area_sqm,
                    json.dumps(merged_tags, ensure_ascii=False),
                    merged_risk_level,
                    ts,
                    property_id,
                ),
            )

        # 2. Upsert auction_round
        stage = normalize_stage(item.stage, item.title)
        status = normalize_status(item.status, bid_count=item.bid_count)
        start_time = normalize_datetime(item.start_time)
        end_time = normalize_datetime(item.end_time)
        ref_price = item.current_price if item.current_price is not None else item.initial_price
        discount_rate = _calc_discount(ref_price, item.consult_price)
        unit_price = _calc_unit_price(ref_price, area_sqm)

        existing_round = self.conn.execute(
            "SELECT * FROM auction_rounds WHERE platform = ? AND item_id = ?",
            (item.platform, item.item_id),
        ).fetchone()

        if existing_round is None:
            cur = self.conn.execute(
                """
                INSERT INTO auction_rounds (
                    property_id, platform, item_id, url, title, stage, status,
                    initial_price, current_price, consult_price, deposit_price,
                    discount_rate, unit_price, bid_count, apply_count, watch_count,
                    start_time, end_time, court_name, raw_json, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    property_id,
                    item.platform,
                    item.item_id,
                    item.url,
                    item.title,
                    stage,
                    status,
                    item.initial_price,
                    item.current_price,
                    item.consult_price,
                    item.deposit_price,
                    discount_rate,
                    unit_price,
                    item.bid_count,
                    item.apply_count,
                    item.watch_count,
                    start_time,
                    end_time,
                    item.court_name,
                    json.dumps(item.raw_payload, ensure_ascii=False),
                    ts,
                    ts,
                ),
            )
            round_id = int(cur.lastrowid)
        else:
            round_id = int(existing_round["id"])
            # Preserve richer fields when list payload has nulls
            new_stage = stage if stage != "未知" else existing_round["stage"]
            new_status = status if status != "unknown" else existing_round["status"]
            new_initial = item.initial_price if item.initial_price is not None else existing_round["initial_price"]
            new_current = item.current_price if item.current_price is not None else existing_round["current_price"]
            new_consult = item.consult_price if item.consult_price is not None else existing_round["consult_price"]
            new_deposit = item.deposit_price if item.deposit_price is not None else existing_round["deposit_price"]
            new_bid = item.bid_count if item.bid_count is not None else existing_round["bid_count"]
            new_apply = item.apply_count if item.apply_count is not None else existing_round["apply_count"]
            new_watch = item.watch_count if item.watch_count is not None else existing_round["watch_count"]
            new_start = start_time or existing_round["start_time"]
            new_end = end_time or existing_round["end_time"]
            new_court = item.court_name or existing_round["court_name"]
            new_ref = new_current if new_current is not None else new_initial
            new_discount = _calc_discount(new_ref, new_consult)
            new_unit = _calc_unit_price(new_ref, area_sqm)

            self.conn.execute(
                """
                UPDATE auction_rounds
                SET property_id = ?,
                    url = ?,
                    title = ?,
                    stage = ?,
                    status = ?,
                    initial_price = ?,
                    current_price = ?,
                    consult_price = ?,
                    deposit_price = ?,
                    discount_rate = ?,
                    unit_price = ?,
                    bid_count = ?,
                    apply_count = ?,
                    watch_count = ?,
                    start_time = ?,
                    end_time = ?,
                    court_name = ?,
                    raw_json = ?,
                    last_seen_at = ?
                WHERE id = ?
                """,
                (
                    property_id,
                    item.url,
                    item.title,
                    new_stage,
                    new_status,
                    new_initial,
                    new_current,
                    new_consult,
                    new_deposit,
                    new_discount,
                    new_unit,
                    new_bid,
                    new_apply,
                    new_watch,
                    new_start,
                    new_end,
                    new_court,
                    json.dumps(item.raw_payload, ensure_ascii=False),
                    ts,
                    round_id,
                ),
            )
            status = new_status
            item_current = new_current
            item_bid = new_bid
            item_apply = new_apply
            item_watch = new_watch
            self._record_snapshot_if_changed(
                round_id=round_id,
                captured_at=ts,
                status=status,
                current_price=item_current,
                bid_count=item_bid,
                apply_count=item_apply,
                watch_count=item_watch,
            )
            self.conn.commit()
            return property_id, round_id, property_created

        self._record_snapshot_if_changed(
            round_id=round_id,
            captured_at=ts,
            status=status,
            current_price=item.current_price,
            bid_count=item.bid_count,
            apply_count=item.apply_count,
            watch_count=item.watch_count,
        )
        self.conn.commit()
        return property_id, round_id, property_created

    def apply_detail(self, detail: RawAuctionDetail) -> bool:
        """Apply fetched detail & announcement data to existing round and its property."""
        try:
            return self._apply_detail_tx(detail)
        except Exception:
            self.conn.rollback()
            raise

    def _apply_detail_tx(self, detail: RawAuctionDetail) -> bool:
        row = self.conn.execute(
            """
            SELECT r.*, p.province, p.city, p.district, p.community_name,
                   p.area_sqm, p.risk_tags_json, p.risk_level, p.property_type,
                   p.is_partial_share, p.ownership_share, p.address
            FROM auction_rounds r
            JOIN properties p ON p.id = r.property_id
            WHERE r.platform = ? AND r.item_id = ?
            """,
            (detail.platform, detail.item_id),
        ).fetchone()
        if row is None:
            return False

        ts = now_str()
        title = detail.title or row["title"]
        parsed = parse_property_info(
            item_id=detail.item_id,
            title=title,
            province=row["province"],
            city=row["city"],
            explicit_district=row["district"],
            address=detail.address or row["address"],
            explicit_area=detail.area_sqm,
            notice_text=detail.notice_text,
            survey_text=detail.survey_text,
        )

        area_sqm = detail.area_sqm or parsed.area_sqm or row["area_sqm"]
        existing_tags = json.loads(row["risk_tags_json"] or "[]")
        merged_tags = list(dict.fromkeys(existing_tags + parsed.risk_tags))
        risk_rank = {"low": 0, "medium": 1, "high": 2}
        merged_risk_level = (
            row["risk_level"]
            if risk_rank.get(row["risk_level"], 0) >= risk_rank.get(parsed.risk_level, 0)
            else parsed.risk_level
        )
        prop_type = parsed.property_type if parsed.property_type != "other" else row["property_type"]

        self.conn.execute(
            """
            UPDATE properties
            SET district = ?,
                community_name = ?,
                address = ?,
                property_type = ?,
                is_partial_share = MAX(is_partial_share, ?),
                ownership_share = ?,
                area_sqm = ?,
                risk_tags_json = ?,
                risk_level = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                parsed.district or row["district"],
                parsed.community_name or row["community_name"],
                detail.address or row["address"],
                prop_type,
                int(parsed.is_partial_share),
                parsed.ownership_share if parsed.is_partial_share else row["ownership_share"],
                area_sqm,
                json.dumps(merged_tags, ensure_ascii=False),
                merged_risk_level,
                ts,
                row["property_id"],
            ),
        )

        new_bid = detail.bid_count if detail.bid_count is not None else row["bid_count"]
        new_stage = normalize_stage(detail.stage, title)
        if new_stage == "未知":
            new_stage = row["stage"]
        new_status = (
            normalize_status(detail.status, bid_count=new_bid)
            if detail.status
            else row["status"]
        )
        if new_status == "unknown":
            new_status = row["status"]

        new_initial = detail.initial_price if detail.initial_price is not None else row["initial_price"]
        new_current = detail.current_price if detail.current_price is not None else row["current_price"]
        if new_initial is not None:
            if new_status == "upcoming":
                new_current = new_initial
            elif (
                new_bid == 0
                and new_current is not None
                and new_current < new_initial
                and new_stage != "变卖"
            ):
                new_current = new_initial
        new_consult = detail.consult_price if detail.consult_price is not None else row["consult_price"]
        new_deposit = detail.deposit_price if detail.deposit_price is not None else row["deposit_price"]
        new_apply = detail.apply_count if detail.apply_count is not None else row["apply_count"]
        new_watch = detail.watch_count if detail.watch_count is not None else row["watch_count"]
        new_start = normalize_datetime(detail.start_time) or row["start_time"]
        new_end = normalize_datetime(detail.end_time) or row["end_time"]
        new_court = detail.court_name or row["court_name"]
        ref_price = new_current if new_current is not None else new_initial

        notice_is_placeholder = (
            bool(detail.notice_text)
            and "加载中" in detail.notice_text
            and len(detail.notice_text.strip()) < 80
        )
        effective_notice = "" if notice_is_placeholder else detail.notice_text
        fetched_ts = None if notice_is_placeholder else ts

        self.conn.execute(
            """
            UPDATE auction_rounds
            SET stage = ?,
                status = ?,
                initial_price = ?,
                current_price = ?,
                consult_price = ?,
                deposit_price = ?,
                discount_rate = ?,
                unit_price = ?,
                bid_count = ?,
                apply_count = ?,
                watch_count = ?,
                start_time = ?,
                end_time = ?,
                court_name = ?,
                notice_text = CASE WHEN ? != '' THEN ? ELSE notice_text END,
                survey_text = CASE WHEN ? != '' THEN ? ELSE survey_text END,
                detail_fetched_at = COALESCE(?, detail_fetched_at),
                last_seen_at = ?
            WHERE id = ?
            """,
            (
                new_stage,
                new_status,
                new_initial,
                new_current,
                new_consult,
                new_deposit,
                _calc_discount(ref_price, new_consult),
                _calc_unit_price(ref_price, area_sqm),
                new_bid,
                new_apply,
                new_watch,
                new_start,
                new_end,
                new_court,
                effective_notice,
                effective_notice,
                detail.survey_text,
                detail.survey_text,
                fetched_ts,
                ts,
                row["id"],
            ),
        )

        self._record_snapshot_if_changed(
            round_id=int(row["id"]),
            captured_at=ts,
            status=new_status,
            current_price=new_current,
            bid_count=new_bid,
            apply_count=new_apply,
            watch_count=new_watch,
        )
        self.conn.commit()
        return True

    def _record_snapshot_if_changed(
        self,
        *,
        round_id: int,
        captured_at: str,
        status: str,
        current_price: float | None,
        bid_count: int | None,
        apply_count: int | None,
        watch_count: int | None,
    ) -> None:
        last = self.conn.execute(
            """
            SELECT status, current_price, bid_count, apply_count, watch_count
            FROM auction_snapshots
            WHERE round_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (round_id,),
        ).fetchone()

        if last is not None and (
            last["status"] == status
            and last["current_price"] == current_price
            and last["bid_count"] == bid_count
            and last["apply_count"] == apply_count
        ):
            return

        self.conn.execute(
            """
            INSERT INTO auction_snapshots (
                round_id, captured_at, status, current_price,
                bid_count, apply_count, watch_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (round_id, captured_at, status, current_price, bid_count, apply_count, watch_count),
        )

    def get_round_statuses(self, platform: str, item_ids: Sequence[str]) -> dict[str, str]:
        """Batch-fetch current status for (platform, item_id) pairs in a single SQL IN query."""
        ids = list(dict.fromkeys(str(item_id) for item_id in item_ids if item_id))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"""
            SELECT item_id, status
            FROM auction_rounds
            WHERE platform = ?
              AND item_id IN ({placeholders})
            """,
            [platform, *ids],
        ).fetchall()
        return {str(row["item_id"]): str(row["status"]) for row in rows}

    def get_round_status(self, platform: str, item_id: str) -> str | None:
        """Fetch current status for a single (platform, item_id), or None if not found."""
        row = self.conn.execute(
            """
            SELECT status
            FROM auction_rounds
            WHERE platform = ? AND item_id = ?
            """,
            (platform, item_id),
        ).fetchone()
        return str(row["status"]) if row is not None else None

    def record_detail_attempt(self, platform: str, item_id: str) -> None:
        """Record a failed detail-enrichment attempt for backoff tracking."""
        self.conn.execute(
            """
            UPDATE auction_rounds
            SET detail_attempts = detail_attempts + 1,
                detail_last_attempt_at = ?
            WHERE platform = ? AND item_id = ?
            """,
            (now_str(), platform, item_id),
        )
        self.conn.commit()

    def record_refresh_attempt(self, platform: str, item_id: str) -> None:
        """Record an ended-refresh attempt for backoff tracking."""
        self.conn.execute(
            """
            UPDATE auction_rounds
            SET refresh_attempts = refresh_attempts + 1,
                refresh_last_attempt_at = ?
            WHERE platform = ? AND item_id = ?
            """,
            (now_str(), platform, item_id),
        )
        self.conn.commit()

    def _has_table(self, table_name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    def get_checkpoint(self, provider: str, region_name: str, scope: str) -> dict[str, Any] | None:
        """Return saved sync checkpoint for (provider, region_name, scope), or None."""
        if not self._has_table("sync_checkpoints"):
            return None
        row = self.conn.execute(
            """
            SELECT next_page, backfill_completed, last_status, updated_at
            FROM sync_checkpoints
            WHERE provider = ? AND region_name = ? AND scope = ?
            """,
            (provider, region_name, scope),
        ).fetchone()
        if row is None:
            return None
        return {
            "next_page": int(row["next_page"]),
            "backfill_completed": bool(row["backfill_completed"]),
            "last_status": str(row["last_status"]),
            "updated_at": str(row["updated_at"]),
        }

    def save_checkpoint(
        self,
        provider: str,
        region_name: str,
        scope: str,
        *,
        next_page: int,
        backfill_completed: bool,
        last_status: str,
    ) -> None:
        """Upsert sync checkpoint for resumable backfill & incremental switching."""
        self.conn.execute(
            """
            INSERT INTO sync_checkpoints (
                provider, region_name, scope, next_page, backfill_completed, last_status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, region_name, scope) DO UPDATE SET
                next_page = excluded.next_page,
                backfill_completed = excluded.backfill_completed,
                last_status = excluded.last_status,
                updated_at = excluded.updated_at
            """,
            (
                provider,
                region_name,
                scope,
                max(1, int(next_page)),
                int(bool(backfill_completed)),
                last_status,
                now_str(),
            ),
        )
        self.conn.commit()

    def list_checkpoints(self) -> list[dict[str, Any]]:
        """List all saved sync checkpoints."""
        if not self._has_table("sync_checkpoints"):
            return []
        rows = self.conn.execute(
            """
            SELECT provider, region_name, scope, next_page, backfill_completed, last_status, updated_at
            FROM sync_checkpoints
            ORDER BY provider ASC, region_name ASC, scope ASC
            """
        ).fetchall()
        return [
            {
                "provider": str(r["provider"]),
                "region_name": str(r["region_name"]),
                "scope": str(r["scope"]),
                "next_page": int(r["next_page"]),
                "backfill_completed": bool(r["backfill_completed"]),
                "last_status": str(r["last_status"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]

    def reset_checkpoints(
        self,
        *,
        provider: str | None = None,
        region_name: str | None = None,
    ) -> int:
        """Delete saved checkpoints (optionally filtered by provider/region)."""
        clauses: list[str] = []
        params: list[Any] = []
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if region_name:
            clauses.append("region_name LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(region_name)}%")
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cur = self.conn.execute(f"DELETE FROM sync_checkpoints {where_sql}", params)
        self.conn.commit()
        return int(cur.rowcount or 0)

    def list_rounds_needing_detail(
        self,
        platform: str,
        limit: int = 20,
        *,
        allowed_property_types: Sequence[str] | None = None,
        allowed_statuses: Sequence[str] | None = None,
        allowed_districts: Sequence[str] | None = None,
        min_date: str | None = None,
        max_attempts: int | None = None,
        cooldown_hours: int = 0,
    ) -> list[sqlite3.Row]:
        """Return rounds whose detail & announcement have never been fetched yet."""
        clauses: list[str] = [
            "r.platform = ?",
            "r.detail_fetched_at IS NULL",
        ]
        params: list[Any] = [platform]

        types = tuple(t for t in (allowed_property_types or ()) if t)
        if types:
            placeholders = ",".join("?" for _ in types)
            clauses.append(f"p.property_type IN ({placeholders})")
            params.extend(types)

        statuses = tuple(s for s in (allowed_statuses or ()) if s)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"r.status IN ({placeholders})")
            params.extend(statuses)

        districts = list(dict.fromkeys(str(d).strip() for d in (allowed_districts or ()) if str(d).strip()))
        if districts:
            if len(districts) == 1:
                clauses.append("p.district LIKE ? ESCAPE '\\'")
                params.append(f"%{escape_like(districts[0])}%")
            else:
                d_clauses = " OR ".join("p.district LIKE ? ESCAPE '\\'" for _ in districts)
                clauses.append(f"({d_clauses})")
                params.extend(f"%{escape_like(d)}%" for d in districts)

        if min_date:
            clauses.append(
                "(r.status IN ('upcoming', 'ongoing') OR COALESCE(r.end_time, r.start_time) IS NULL "
                "OR COALESCE(r.end_time, r.start_time) >= ?)"
            )
            params.append(min_date)

        if max_attempts is not None and max_attempts > 0:
            clauses.append("r.detail_attempts < ?")
            params.append(max_attempts)

        if cooldown_hours > 0:
            cutoff = (datetime.now(CN_TZ) - timedelta(hours=cooldown_hours)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            clauses.append("(r.detail_last_attempt_at IS NULL OR r.detail_last_attempt_at <= ?)")
            params.append(cutoff)

        where_sql = "\n              AND ".join(clauses)
        params.append(limit)
        return self.conn.execute(
            f"""
            SELECT r.id, r.platform, r.item_id, r.url, r.title, p.city
            FROM auction_rounds r
            JOIN properties p ON p.id = r.property_id
            WHERE {where_sql}
            ORDER BY
              CASE r.status WHEN 'ongoing' THEN 0 WHEN 'upcoming' THEN 1 ELSE 2 END,
              r.first_seen_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    def list_rounds_needing_ended_refresh(
        self,
        platform: str,
        limit: int = 20,
        *,
        allowed_property_types: Sequence[str] | None = None,
        min_end_time: str | None = None,
        max_attempts: int | None = None,
        cooldown_hours: int = 0,
    ) -> list[sqlite3.Row]:
        """Return rounds whose end_time has passed but status is still non-terminal."""
        clauses: list[str] = [
            "r.platform = ?",
            "r.status IN ('upcoming', 'ongoing', 'ended', 'unknown')",
            "r.end_time IS NOT NULL",
            "r.end_time <= ?",
        ]
        params: list[Any] = [platform, now_str()]

        types = tuple(t for t in (allowed_property_types or ()) if t)
        if types:
            placeholders = ",".join("?" for _ in types)
            clauses.append(f"p.property_type IN ({placeholders})")
            params.extend(types)

        if min_end_time:
            clauses.append("r.end_time >= ?")
            params.append(min_end_time)

        if max_attempts is not None and max_attempts > 0:
            clauses.append("r.refresh_attempts < ?")
            params.append(max_attempts)

        if cooldown_hours > 0:
            cutoff = (datetime.now(CN_TZ) - timedelta(hours=cooldown_hours)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            clauses.append("(r.refresh_last_attempt_at IS NULL OR r.refresh_last_attempt_at <= ?)")
            params.append(cutoff)

        where_sql = "\n              AND ".join(clauses)
        params.append(limit)
        return self.conn.execute(
            f"""
            SELECT r.id, r.platform, r.item_id, r.url, r.title, r.end_time
            FROM auction_rounds r
            JOIN properties p ON p.id = r.property_id
            WHERE {where_sql}
            ORDER BY r.end_time DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    @staticmethod
    def _classify_round_property_type(row: sqlite3.Row) -> str:
        """Re-evaluate property type using latest classifier while preserving detail-verified non-residential rows."""
        stored = str(row["property_type"] or "other")
        if row["detail_fetched_at"] and stored != "residential":
            return stored

        raw_obj: dict[str, Any] = {}
        try:
            parsed_raw = json.loads(row["raw_json"] or "{}")
            if isinstance(parsed_raw, dict):
                raw_obj = parsed_raw
        except (ValueError, TypeError):
            raw_obj = {}

        data = raw_obj.get("data") if isinstance(raw_obj.get("data"), dict) else raw_obj
        ha = data.get("houseAttributes") if isinstance(data.get("houseAttributes"), dict) else {}
        purpose_hint = str(ha.get("housePurpose") or data.get("housePurpose") or "").strip()
        cat_hint = str(data.get("category") or data.get("categoryId") or "").strip()
        if not cat_hint and stored == "residential":
            platform = str(row["platform"] or "")
            if platform == "jd":
                cat_hint = "12728"
            elif platform == "taobao":
                cat_hint = "50025969"
            else:
                cat_hint = "住宅用房"

        notice_text = str(row["notice_text"] or "")
        if _has_captcha_notice(notice_text):
            notice_text = ""
        detail_text = f"{row['address'] or ''}\n{notice_text}\n{row['survey_text'] or ''}".strip()

        reclass = classify_property_type(
            str(row["title"] or ""),
            detail_text=detail_text,
            category_hint=cat_hint,
            purpose_hint=purpose_hint,
        )
        if stored != "residential" and reclass == "residential":
            if "住宅" in purpose_hint or stored == "industrial":
                return reclass
            return stored
        return reclass

    def cleanup_stale_data(
        self,
        *,
        min_date: str | None = None,
        allowed_property_types: Sequence[str] | None = None,
        reset_captcha_details: bool = True,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Preview or clean up expired rounds, non-target property types, captcha pollution, and revocation status."""
        allowed_types = {
            str(t).strip().lower() for t in (allowed_property_types or ()) if str(t).strip()
        }

        rows = self.conn.execute(
            """
            SELECT r.id, r.property_id, r.platform, r.title, r.status,
                   r.start_time, r.end_time, r.notice_text, r.survey_text,
                   r.detail_fetched_at, r.raw_json,
                   p.property_type, p.address
            FROM auction_rounds r
            JOIN properties p ON p.id = r.property_id
            """
        ).fetchall()

        expired_ids: set[int] = set()
        non_target_ids: set[int] = set()

        for row in rows:
            rid = int(row["id"])
            if min_date:
                ref_dt = row["end_time"] or row["start_time"]
                if (
                    row["status"] not in ("upcoming", "ongoing")
                    and ref_dt is not None
                    and ref_dt < min_date
                ):
                    expired_ids.add(rid)

            if allowed_types:
                eff_type = self._classify_round_property_type(row)
                if eff_type not in allowed_types:
                    non_target_ids.add(rid)

        delete_ids = expired_ids | non_target_ids

        all_prop_ids = {
            int(r["id"]) for r in self.conn.execute("SELECT id FROM properties").fetchall()
        }
        kept_prop_ids = {
            int(row["property_id"]) for row in rows if int(row["id"]) not in delete_ids
        }
        orphan_prop_ids = all_prop_ids - kept_prop_ids

        captcha_reset_ids: set[int] = set()
        revocation_fix_ids: set[int] = set()

        for row in rows:
            rid = int(row["id"])
            if rid in delete_ids:
                continue
            if reset_captcha_details and _has_captcha_notice(row["notice_text"]):
                captcha_reset_ids.add(rid)
            if row["status"] == "unknown" and '"revocation"' in str(row["raw_json"] or "").lower():
                revocation_fix_ids.add(rid)

        if not dry_run:
            try:
                for chunk in _iter_chunks(sorted(delete_ids)):
                    placeholders = ",".join("?" for _ in chunk)
                    self.conn.execute(
                        f"DELETE FROM auction_rounds WHERE id IN ({placeholders})",
                        chunk,
                    )

                for chunk in _iter_chunks(sorted(orphan_prop_ids)):
                    placeholders = ",".join("?" for _ in chunk)
                    self.conn.execute(
                        f"DELETE FROM properties WHERE id IN ({placeholders})",
                        chunk,
                    )

                if reset_captcha_details:
                    for chunk in _iter_chunks(sorted(captcha_reset_ids)):
                        placeholders = ",".join("?" for _ in chunk)
                        self.conn.execute(
                            f"""
                            UPDATE auction_rounds
                            SET detail_fetched_at = NULL,
                                notice_text = '',
                                survey_text = '',
                                detail_attempts = 0,
                                detail_last_attempt_at = NULL
                            WHERE id IN ({placeholders})
                            """,
                            chunk,
                        )

                for chunk in _iter_chunks(sorted(revocation_fix_ids)):
                    placeholders = ",".join("?" for _ in chunk)
                    self.conn.execute(
                        f"""
                        UPDATE auction_rounds
                        SET status = 'withdrawn'
                        WHERE id IN ({placeholders})
                        """,
                        chunk,
                    )

                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

        return {
            "expired_rounds": len(expired_ids),
            "non_target_type_rounds": len(non_target_ids),
            "total_delete_rounds": len(delete_ids),
            "orphan_properties": len(orphan_prop_ids),
            "captcha_reset_rounds": len(captcha_reset_ids),
            "revocation_fixed_rounds": len(revocation_fix_ids),
            "dry_run": dry_run,
        }

    def recompute_missing_areas(self) -> dict[str, int]:
        """Re-extract missing area_sqm from stored texts and backfill unit_price locally."""
        ts = now_str()
        rows = self.conn.execute(
            """
            SELECT r.id AS round_id, r.property_id, r.title, r.notice_text, r.survey_text,
                   r.initial_price, r.current_price, r.unit_price,
                   p.address, p.area_sqm
            FROM auction_rounds r
            JOIN properties p ON p.id = r.property_id
            WHERE p.area_sqm IS NULL OR p.area_sqm <= 0 OR r.unit_price IS NULL
            ORDER BY r.id DESC
            """
        ).fetchall()

        prop_area_map: dict[int, float] = {}
        areas_filled = 0
        unit_prices_filled = 0

        for row in rows:
            pid = int(row["property_id"])
            area = row["area_sqm"]
            if (area is None or float(area) <= 0) and pid in prop_area_map:
                area = prop_area_map[pid]
            if area is None or float(area) <= 0:
                extracted = extract_area_sqm(
                    str(row["title"] or ""),
                    f"{row['address'] or ''}\n{row['notice_text'] or ''}\n{row['survey_text'] or ''}",
                )
                if extracted is not None and extracted > 0:
                    area = extracted
                    prop_area_map[pid] = extracted
                    self.conn.execute(
                        "UPDATE properties SET area_sqm = ?, updated_at = ? WHERE id = ?",
                        (area, ts, pid),
                    )
                    areas_filled += 1

            if area is not None and float(area) > 0:
                ref_price = row["current_price"] if row["current_price"] is not None else row["initial_price"]
                new_unit = _calc_unit_price(ref_price, float(area))
                if new_unit is not None and row["unit_price"] is None:
                    self.conn.execute(
                        "UPDATE auction_rounds SET unit_price = ? WHERE id = ?",
                        (new_unit, int(row["round_id"])),
                    )
                    unit_prices_filled += 1

        if areas_filled or unit_prices_filled:
            self.conn.commit()
        return {"areas_filled": areas_filled, "unit_prices_filled": unit_prices_filled}
