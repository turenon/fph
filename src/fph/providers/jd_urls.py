from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fph.config import RegionConfig

JD_DEFAULT_RESIDENTIAL_CATEGORY = "12728"

# Verified via api.m.jd.com getAreaInfoMap
JD_PROVINCE_IDS: dict[str, str] = {
    "北京": "1",
    "上海": "2",
    "天津": "3",
    "重庆": "4",
    "河北": "5",
    "山西": "6",
    "河南": "7",
    "辽宁": "8",
    "吉林": "9",
    "黑龙江": "10",
    "内蒙古": "11",
    "江苏": "12",
    "山东": "13",
    "安徽": "14",
    "浙江": "15",
    "福建": "16",
    "湖北": "17",
    "湖南": "18",
    "广东": "19",
    "广西": "20",
    "江西": "21",
    "四川": "22",
    "海南": "23",
    "贵州": "24",
    "云南": "25",
    "西藏": "26",
    "陕西": "27",
    "甘肃": "28",
    "青海": "29",
    "宁夏": "30",
    "新疆": "31",
    "台湾": "32",
    "港澳": "52993",
}

# Verified via api.m.jd.com getAreaInfoMap (provinceId=16 Fujian)
JD_CITY_IDS: dict[str, str] = {
    "福州": "1303",
    "厦门": "1315",
    "三明": "1317",
    "莆田": "1329",
    "泉州": "1332",
    "漳州": "1341",
    "南平": "1352",
    "龙岩": "1362",
    "宁德": "1370",
}

# JD pmsearch spo_multiStatus mapping (verified from pmsearch bundle: Mc={0:"102",1:"101",2:"105",5:"108",6:"106",7:"107"})
JD_STATUS_SCOPE_TO_PARAMS: dict[str, list[tuple[str, str]]] = {
    "active": [("ongoing", "101"), ("upcoming", "102")],
    "ongoing": [("ongoing", "101")],
    "upcoming": [("upcoming", "102")],
    "ended": [("ended", "105")],
    "all": [("all", "")],
}

_LEGACY_STATUS_MAP: dict[str, str] = {
    "0": "102",
    "1": "101",
    "2": "105",
    "5": "108",
    "6": "106",
    "7": "107",
}


def _clean_region_name(name: str) -> str:
    s = (name or "").strip()
    for suffix in ("特别行政区", "自治区", "省", "市"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
            break
    return s


def resolve_jd_province_id(region: RegionConfig) -> str:
    if region.jd_province_id.strip():
        return region.jd_province_id.strip()
    return JD_PROVINCE_IDS.get(_clean_region_name(region.province), "")


def resolve_jd_city_id(region: RegionConfig) -> str:
    if region.jd_city_id.strip():
        return region.jd_city_id.strip()
    return JD_CITY_IDS.get(_clean_region_name(region.city), "")


def resolve_jd_category_id(region: RegionConfig) -> str:
    if region.jd_category_id.strip():
        return region.jd_category_id.strip()
    cat = (region.category or "").strip()
    if cat and cat != "50025969":
        return cat
    return JD_DEFAULT_RESIDENTIAL_CATEGORY


def normalize_jd_status_param(status_scope: str) -> str:
    s = (status_scope or "").strip()
    if s in {"101", "102", "105", "106", "107", "108"}:
        return s
    if s in _LEGACY_STATUS_MAP:
        return _LEGACY_STATUS_MAP[s]
    if s == "ongoing":
        return "101"
    if s == "upcoming":
        return "102"
    if s == "ended":
        return "105"
    return ""


def expand_jd_status_queries(status_scope: str) -> list[tuple[str, str]]:
    """Expand logical scope ('active', 'ended', etc.) into (label, spo_multiStatus) pairs."""
    return JD_STATUS_SCOPE_TO_PARAMS.get(status_scope, [("all", "")])


def build_jd_list_url(
    region: RegionConfig,
    st_param: str = "",
    page: int = 1,  # noqa: ARG001 - pmsearch paginates client-side
) -> str:
    """Build a pmsearch.jd.com judicial auction search URL for the given region & status."""
    mapped_status = normalize_jd_status_param(st_param)

    if region.custom_url:
        parts = urlsplit(region.custom_url)
        query_items = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in {"spo_multiStatus", "paimaiStatus"}
        ]
        if mapped_status:
            query_items.append(("spo_multiStatus", mapped_status))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment))

    params: list[tuple[str, str]] = [
        ("pageParam", "pc_search_judicature"),
        ("publishSource", "7"),
    ]
    prov_id = resolve_jd_province_id(region)
    city_id = resolve_jd_city_id(region)
    county_id = region.jd_county_id.strip()
    cat_id = resolve_jd_category_id(region)

    if prov_id:
        params.append(("spo_multiProvinceIds", prov_id))
    if city_id:
        params.append(("spo_multiCityIds", city_id))
    if county_id:
        params.append(("spo_multiCountyIds", county_id))
    if cat_id:
        params.append(("spo_multiThirdCateIds", cat_id))
    if mapped_status:
        params.append(("spo_multiStatus", mapped_status))

    return f"https://pmsearch.jd.com/?{urlencode(params)}"


def build_jd_item_url(item_id: str) -> str:
    clean_id = str(item_id).strip()
    return f"https://paimai.jd.com/{clean_id}"
