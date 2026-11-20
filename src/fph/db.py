from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from fph.models import RawAuctionDetail, RawAuctionItem, SyncSummary
from fph.normalizer import (
    normalize_datetime,
    normalize_stage,
    normalize_status,
    parse_property_info,
)

CN_TZ = timezone(timedelta(hours=8))

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


class AuctionDB:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
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
        ts = now_str()
        parsed = parse_property_info(
            item_id=item.item_id,
            title=item.title,
            province=item.province,
            city=item.city,
            explicit_district=item.district,
            category_hint=item.category_hint,
        )

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
                    parsed.community_name,
                    parsed.normalized_address,
                    item.title,
                    parsed.property_type,
                    int(parsed.is_partial_share),
                    parsed.ownership_share,
                    parsed.area_sqm,
                    json.dumps(parsed.risk_tags, ensure_ascii=False),
                    parsed.risk_level,
                    ts,
                    ts,
                ),
            )
            property_id = int(cur.lastrowid)
            area_sqm = parsed.area_sqm
            property_created = True
        else:
            property_id = int(row["id"])
            area_sqm = row["area_sqm"] if row["area_sqm"] is not None else parsed.area_sqm
            district = row["district"] or parsed.district
            community = row["community_name"] or parsed.community_name
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
        new_status = (
            normalize_status(detail.status, bid_count=new_bid)
            if detail.status
            else row["status"]
        )
        if new_status == "unknown":
            new_status = row["status"]

        new_initial = detail.initial_price if detail.initial_price is not None else row["initial_price"]
        new_current = detail.current_price if detail.current_price is not None else row["current_price"]
        new_consult = detail.consult_price if detail.consult_price is not None else row["consult_price"]
        new_deposit = detail.deposit_price if detail.deposit_price is not None else row["deposit_price"]
        new_apply = detail.apply_count if detail.apply_count is not None else row["apply_count"]
        new_watch = detail.watch_count if detail.watch_count is not None else row["watch_count"]
        new_start = normalize_datetime(detail.start_time) or row["start_time"]
        new_end = normalize_datetime(detail.end_time) or row["end_time"]
        new_court = detail.court_name or row["court_name"]
        ref_price = new_current if new_current is not None else new_initial

        self.conn.execute(
            """
            UPDATE auction_rounds
            SET status = ?,
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
                detail_fetched_at = ?,
                last_seen_at = ?
            WHERE id = ?
            """,
            (
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
                detail.notice_text,
                detail.notice_text,
                detail.survey_text,
                detail.survey_text,
                ts,
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

    def list_rounds_needing_detail(self, platform: str, limit: int = 20) -> list[sqlite3.Row]:
        """Return rounds whose detail & announcement have never been fetched yet."""
        return self.conn.execute(
            """
            SELECT r.id, r.platform, r.item_id, r.url, r.title, p.city
            FROM auction_rounds r
            JOIN properties p ON p.id = r.property_id
            WHERE r.platform = ?
              AND r.detail_fetched_at IS NULL
            ORDER BY
              CASE r.status WHEN 'ongoing' THEN 0 WHEN 'upcoming' THEN 1 ELSE 2 END,
              r.first_seen_at DESC
            LIMIT ?
            """,
            (platform, limit),
        ).fetchall()

    def list_rounds_needing_ended_refresh(self, platform: str, limit: int = 20) -> list[sqlite3.Row]:
        """Return rounds whose end_time has passed but status is still non-terminal."""
        return self.conn.execute(
            """
            SELECT r.id, r.platform, r.item_id, r.url, r.title, r.end_time
            FROM auction_rounds r
            WHERE r.platform = ?
              AND r.status IN ('upcoming', 'ongoing', 'ended', 'unknown')
              AND r.end_time IS NOT NULL
              AND r.end_time <= ?
            ORDER BY r.end_time DESC
            LIMIT ?
            """,
            (platform, now_str(), limit),
        ).fetchall()
