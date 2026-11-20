from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from fph.db import AuctionDB


def get_market_summary(
    db: AuctionDB,
    *,
    city: str | None = None,
    by_district: bool = False,
    by_month: bool = False,
) -> list[dict[str, Any]]:
    """Aggregate residential (full-ownership) market metrics:
    listed count, sold/failed counts, clearance rate, avg discount rate, avg unit price.
    """
    group_cols = ["city"]
    if by_district:
        group_cols.append("district")
    if by_month:
        group_cols.append("substr(COALESCE(end_time, start_time, last_seen_at), 1, 7) AS month")

    select_group = ", ".join(group_cols)
    group_by = ", ".join(
        col.split(" AS ")[-1] for col in group_cols
    )

    where_clauses = ["property_type = 'residential'", "is_partial_share = 0"]
    params: list[Any] = []
    if city:
        where_clauses.append("city LIKE ?")
        params.append(f"%{city.replace('市', '')}%")

    sql = f"""
    SELECT
        {select_group},
        COUNT(*) AS total_rounds,
        COUNT(DISTINCT property_id) AS unique_properties,
        SUM(CASE WHEN status IN ('upcoming', 'ongoing') THEN 1 ELSE 0 END) AS active_count,
        SUM(CASE WHEN status = 'sold' THEN 1 ELSE 0 END) AS sold_count,
        SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
        ROUND(
            100.0 * SUM(CASE WHEN status = 'sold' THEN 1 ELSE 0 END)
            / NULLIF(SUM(CASE WHEN status IN ('sold', 'failed') THEN 1 ELSE 0 END), 0),
            2
        ) AS clearance_rate_pct,
        ROUND(
            AVG(CASE WHEN status = 'sold' AND discount_rate IS NOT NULL THEN discount_rate END),
            4
        ) AS avg_sold_discount,
        ROUND(
            AVG(CASE WHEN status = 'sold' AND unit_price IS NOT NULL THEN unit_price END),
            2
        ) AS avg_sold_unit_price,
        ROUND(
            AVG(CASE WHEN status IN ('upcoming', 'ongoing') AND unit_price IS NOT NULL THEN unit_price END),
            2
        ) AS avg_active_unit_price
    FROM v_auction_full
    WHERE {' AND '.join(where_clauses)}
    GROUP BY {group_by}
    ORDER BY {group_by}
    """
    rows = db.conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def find_bargain_candidates(
    db: AuctionDB,
    *,
    city: str | None = None,
    district: str | None = None,
    max_discount: float = 0.80,
    max_total_wan: float | None = None,
    min_area: float | None = None,
    max_risk_level: str = "medium",
    stage: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Find active residential full-ownership bargain candidates sorted by attractiveness."""
    allowed_risks = {
        "low": ("low",),
        "medium": ("low", "medium"),
        "high": ("low", "medium", "high"),
    }.get(max_risk_level, ("low", "medium"))

    where = [
        f"risk_level IN ({','.join('?' for _ in allowed_risks)})",
        "(discount_rate IS NULL OR discount_rate <= ?)",
    ]
    params: list[Any] = list(allowed_risks) + [max_discount]

    if city:
        where.append("city LIKE ?")
        params.append(f"%{city.replace('市', '')}%")
    if district:
        where.append("district LIKE ?")
        params.append(f"%{district}%")
    if max_total_wan is not None:
        where.append("COALESCE(current_price, initial_price) <= ?")
        params.append(max_total_wan * 10_000)
    if min_area is not None:
        where.append("area_sqm >= ?")
        params.append(min_area)
    if stage:
        where.append("stage = ?")
        params.append(stage)

    sql = f"""
    SELECT
        city,
        district,
        community_name,
        title,
        stage,
        status,
        ROUND(COALESCE(current_price, initial_price) / 10000.0, 2) AS price_wan,
        ROUND(consult_price / 10000.0, 2) AS consult_wan,
        discount_rate,
        area_sqm,
        unit_price,
        apply_count,
        bid_count,
        total_rounds_for_property,
        risk_level,
        risk_tags_json,
        start_time,
        end_time,
        url
    FROM v_bargain_candidates
    WHERE {' AND '.join(where)}
    ORDER BY
        CASE WHEN stage IN ('二拍', '变卖') THEN 0 ELSE 1 END,
        COALESCE(discount_rate, 9.99) ASC,
        COALESCE(apply_count, 999) ASC,
        end_time ASC
    LIMIT ?
    """
    params.append(limit)
    rows = db.conn.execute(sql, params).fetchall()
    result: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        item["risk_tags"] = json.loads(item.pop("risk_tags_json") or "[]")
        result.append(item)
    return result


def export_dataset(
    db: AuctionDB,
    output_path: str | Path,
    *,
    dataset: str = "all",
    city: str | None = None,
    fmt: str = "csv",
) -> int:
    """Export full / deals / bargains dataset to CSV (UTF-8-BOM for Windows Excel) or JSON."""
    view_map = {
        "all": "v_auction_full",
        "deals": "v_residential_deals",
        "bargains": "v_bargain_candidates",
    }
    if dataset not in view_map:
        raise ValueError(f"不支持的数据集: {dataset}（可选: all / deals / bargains）")

    where_sql = ""
    params: list[Any] = []
    if city:
        where_sql = "WHERE city LIKE ?"
        params.append(f"%{city.replace('市', '')}%")

    sql = f"SELECT * FROM {view_map[dataset]} {where_sql} ORDER BY COALESCE(end_time, last_seen_at) DESC"
    rows = [dict(r) for r in db.conn.execute(sql, params).fetchall()]

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "json":
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(rows)

    if fmt == "csv":
        # utf-8-sig ensures Excel on Windows opens Chinese text without mojibake
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            if not rows:
                f.write("")
                return 0
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)

    raise ValueError(f"不支持的导出格式: {fmt}（可选: csv / json）")
