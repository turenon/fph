from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from fph.config import RegionConfig
from fph.normalizer import CN_TZ

# Taobao judicial auction path status slot mapping (/list/{category}__{status}...):
# 0: ongoing (正在进行), 1: upcoming (即将开始), 2: ended (已结束), -1: all (不限)
# Note: Query parameter `st_param` is the sort order (`st_param=4` = 最新发布 / newest first).
STATUS_SCOPE_TO_ST_PARAMS: dict[str, list[tuple[str, str]]] = {
    "active": [("ongoing", "0"), ("upcoming", "1")],
    "ongoing": [("ongoing", "0")],
    "upcoming": [("upcoming", "1")],
    "ended": [("ended", "2")],
    "all": [("all", "-1")],
}

DEFAULT_SORT_PARAM = "4"

_LOGICAL_STATUS_TO_PATH_CODE: dict[str, str] = {
    "ongoing": "0",
    "upcoming": "1",
    "ended": "2",
    "all": "-1",
}


def encode_gbk_query_value(text: str) -> str:
    """Taobao sf.taobao.com city/province path and query segments use GBK percent-encoding."""
    if not text:
        return ""
    return quote(text.encode("gbk"))


def _normalize_path_status(st_param: str) -> str:
    raw = (st_param or "").strip().lower()
    if not raw:
        return "-1"
    return _LOGICAL_STATUS_TO_PATH_CODE.get(raw, raw)


def _clean_city_name(city: str) -> str:
    text = (city or "").strip()
    if text.endswith("市") and len(text) > 1:
        return text[:-1].strip()
    return text


def _clean_province_name(province: str) -> str:
    text = (province or "").strip()
    if text.endswith("省") and len(text) > 1:
        return text[:-1].strip()
    return text


def build_list_url(
    region: RegionConfig,
    st_param: str,
    page: int = 1,
    *,
    lookback_days: int | None = None,
    reference_date: date | None = None,
) -> str:
    """Build a sf.taobao.com list URL for the given region, path status code, and page.

    `st_param` represents the path status slot code ("0"=ongoing, "1"=upcoming,
    "2"=ended, "-1"=all) and keeps its parameter name for backwards compatibility.
    """
    status_code = _normalize_path_status(st_param)
    apply_lookback = status_code == "2" and bool(lookback_days and lookback_days > 0)
    if apply_lookback:
        today = reference_date or datetime.now(CN_TZ).date()
        start_from = (today - timedelta(days=int(lookback_days))).isoformat()  # type: ignore[arg-type]
        start_to = today.isoformat()
    else:
        start_from = ""
        start_to = ""

    if region.custom_url:
        parts = urlsplit(region.custom_url)
        raw_pairs = [seg for seg in parts.query.split("&") if seg]
        kept_pairs = [seg for seg in raw_pairs if seg.split("=", 1)[0] != "page"]
        existing_keys = {seg.split("=", 1)[0] for seg in kept_pairs}
        if apply_lookback and "auction_start_from" not in existing_keys:
            kept_pairs = [
                seg
                for seg in kept_pairs
                if seg.split("=", 1)[0] not in {"auction_start_seg", "auction_start_from", "auction_start_to"}
            ]
            kept_pairs.extend(
                [
                    "auction_start_seg=0",
                    f"auction_start_from={start_from}",
                    f"auction_start_to={start_to}",
                ]
            )
        if page > 1:
            kept_pairs.append(f"page={page}")
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(kept_pairs), parts.fragment))

    category = (region.category or "50025969").strip() or "50025969"
    city_clean = _clean_city_name(region.city)
    province_clean = _clean_province_name(region.province)

    params: list[tuple[str, str]] = []
    if city_clean:
        path = f"/list/{category}__{status_code}___{encode_gbk_query_value(city_clean)}.htm"
    elif province_clean:
        path = f"/list/{category}__{status_code}__{encode_gbk_query_value(province_clean)}.htm"
    else:
        path = f"/list/{category}__{status_code}.htm"
        if region.location_code:
            params.append(("location_code", region.location_code.strip()))

    params.append(("auction_source", "0"))
    params.append(("st_param", DEFAULT_SORT_PARAM))
    if apply_lookback:
        params.append(("auction_start_seg", "0"))
        params.append(("auction_start_from", start_from))
        params.append(("auction_start_to", start_to))
    else:
        params.append(("auction_start_seg", "-1"))

    if page > 1:
        params.append(("page", str(page)))

    return f"https://sf.taobao.com{path}?{urlencode(params)}"


def expand_status_queries(status_scope: str) -> list[tuple[str, str]]:
    """Expand logical scope ('active', 'ended', etc.) into (label, path_status_code) pairs."""
    return STATUS_SCOPE_TO_ST_PARAMS.get(status_scope, [("all", "-1")])
