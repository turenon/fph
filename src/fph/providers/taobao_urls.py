from __future__ import annotations

from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from fph.config import RegionConfig

# Taobao judicial auction status parameter mapping:
# -1: all, 0: upcoming (即将开始), 1: ongoing (正在进行), 2: ended (已结束), 3: suspended, 4: withdrawn
STATUS_SCOPE_TO_ST_PARAMS: dict[str, list[tuple[str, str]]] = {
    "active": [("ongoing", "1"), ("upcoming", "0")],
    "ongoing": [("ongoing", "1")],
    "upcoming": [("upcoming", "0")],
    "ended": [("ended", "2")],
    "all": [("all", "-1")],
}


def encode_gbk_query_value(text: str) -> str:
    """Taobao sf.taobao.com city/province params use GBK percent-encoding."""
    if not text:
        return ""
    return quote(text.encode("gbk"))


def build_list_url(
    region: RegionConfig,
    st_param: str,
    page: int = 1,
) -> str:
    """Build a sf.taobao.com list URL for the given region, status param, and page."""
    if region.custom_url:
        parts = urlsplit(region.custom_url)
        query_items = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in {"page", "st_param"}]
        query_items.append(("st_param", st_param))
        if page > 1:
            query_items.append(("page", str(page)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment))

    category = region.category or "50025969"

    # Prefer /list/{category}.htm?location_code=... when location_code is provided (exact administrative division)
    if region.location_code:
        params: list[tuple[str, str]] = [
            ("location_code", region.location_code),
            ("auction_source", "0"),
            ("st_param", st_param),
            ("auction_start_seg", "-1"),
        ]
        if page > 1:
            params.append(("page", str(page)))
        return f"https://sf.taobao.com/list/{category}.htm?{urlencode(params)}"

    # Fallback to item_list.htm with GBK-encoded province/city
    raw_pairs = [
        f"category={category}",
        "auction_source=0",
        f"province={encode_gbk_query_value(region.province)}",
        f"city={encode_gbk_query_value(region.city)}",
        f"st_param={st_param}",
        "auction_start_seg=-1",
    ]
    if page > 1:
        raw_pairs.append(f"page={page}")
    return f"https://sf.taobao.com/item_list.htm?{'&'.join(raw_pairs)}"


def expand_status_queries(status_scope: str) -> list[tuple[str, str]]:
    """Expand logical scope ('active', 'ended', etc.) into (label, st_param) pairs."""
    return STATUS_SCOPE_TO_ST_PARAMS.get(status_scope, [("all", "-1")])
