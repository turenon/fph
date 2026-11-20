from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone, timedelta

from fph.models import ParsedPropertyInfo

CN_TZ = timezone(timedelta(hours=8))

CITY_DISTRICTS: dict[str, tuple[str, ...]] = {
    "福州": (
        "鼓楼区",
        "台江区",
        "仓山区",
        "晋安区",
        "马尾区",
        "长乐区",
        "闽侯县",
        "连江县",
        "罗源县",
        "闽清县",
        "永泰县",
        "福清市",
        "平潭县",
        "平潭综合实验区",
        "高新区",
    ),
    "龙岩": (
        "新罗区",
        "永定区",
        "长汀县",
        "上杭县",
        "武平县",
        "连城县",
        "漳平市",
    ),
}

STAGE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("变卖", ("变卖", "【变卖】", "（变卖）")),
    ("二拍", ("二拍", "第二次拍卖", "第2次拍卖", "【二拍】", "（二拍）")),
    ("一拍", ("一拍", "第一次拍卖", "第1次拍卖", "【一拍】", "（一拍）")),
    ("重新拍卖", ("重新拍卖", "重拍")),
]

RISK_RULES: list[tuple[str, str, str]] = [
    # (tag, severity, regex)
    ("不交吉/不腾退", "high", r"(不负责腾退|不予清场|不交吉|自行腾退|现状交付.{0,12}不负责清空|法院不负责清场)"),
    ("有人占用", "high", r"(有人居住|有人占用|被执行人.{0,8}居住|案外人.{0,8}占用|未腾空|尚未腾空)"),
    ("带租约", "high", r"(带租拍卖|有租赁|存在租赁|租赁合同.{0,12}(有效|未到期)|买卖不破租赁|承租人)"),
    ("部分产权", "high", r"(产权份额|共有份额|占\s*\d+(?:\.\d+)?%\s*(?:产权|份额)|(?:二分之一|三分之一|四分之一|1/2|1/3|1/4|50%|25%)\s*(?:产权|份额))"),
    ("无证/产权瑕疵", "high", r"(无房产证|无不动产权证|未办理产权证|未取得产权证|小产权|无法办理过户|权属瑕疵)"),
    ("非正常死亡/凶宅", "high", r"(非正常死亡|自杀|他杀|凶宅|命案)"),
    ("买受人全额担税", "medium", r"(一切税费.{0,10}买受人|全部税费.{0,10}买受人|买卖双方.{0,8}税费.{0,10}均由买受人|税费均由买受人承担)"),
    ("欠费风险", "medium", r"(欠缴.{0,10}(物业|水电|燃气|取暖)|物业费.{0,10}买受人承担|拖欠物业费)"),
    ("违建/搭建", "medium", r"(违章建筑|违法建筑|违建|自行拆除|搭建物|改建)"),
    ("划拨土地", "medium", r"(划拨土地|土地性质.{0,6}划拨|需补缴土地出让金)"),
    ("有户口风险", "low", r"(户口.{0,12}不负责迁出|户籍.{0,12}自行解决)"),
]

TITLE_NOISE_PATTERNS = [
    r"[【\[\(（]\s*(?:一拍|二拍|三拍|变卖|重新拍卖|破|破产|涉刑|不限购|可贷款|捡漏|急售|降价|精选)[^】\]\)）]*[】\]\)）]",
    r"[（\(][^（）\(\)]*(?:详见评估报告|含室内|不含室内|按现状|现状拍卖|含家具|不含家具|附带|一拍|二拍|变卖)[^（）\(\)]*[）\)]",
    r"(?:关于拍卖|公开拍卖|司法拍卖|拍卖|变卖|位于|坐落于|坐落|标的物[:：]?)",
    r"(?:不动产(?:权)?|房产(?:一套|1套)?|房屋(?:一套|1套)?|住宅(?:一套|1套)?|房地产(?:一宗|一套)?)(?:的)?$",
    r"不动产单元号[:：]?\s*[A-Za-z0-9]+",
    r"不动产权证号[:：]?\s*[^\s，,。]+",
]

COMMUNITY_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9·]{2,16}(?:小区|花园|家园|雅苑|锦园|公馆|御府|华府|学府|名城|新城|广场|大厦|别墅|尚书院|世家|首府|府邸|茗居|豪庭|国际城|印象|中心|一期|二期|三期|四期|苑|庭|轩|阁|湾|郡|府|城|村))"
)

BUILDING_AREA_PATTERNS = [
    re.compile(r"建筑面积[：:约为\s]*([0-9]+(?:\.[0-9]+)?)\s*(?:平方米|平米|㎡|m²|m2)", re.IGNORECASE),
    re.compile(r"房屋面积[：:约为\s]*([0-9]+(?:\.[0-9]+)?)\s*(?:平方米|平米|㎡|m²|m2)", re.IGNORECASE),
    re.compile(r"套内(?:建筑)?面积[：:约为\s]*([0-9]+(?:\.[0-9]+)?)\s*(?:平方米|平米|㎡|m²|m2)", re.IGNORECASE),
    re.compile(r"(?<!土地)(?<!占地)(?<!宗地)面积[：:约为\s]*([0-9]+(?:\.[0-9]+)?)\s*(?:平方米|平米|㎡|m²|m2)", re.IGNORECASE),
]

OWNERSHIP_SHARE_RE = re.compile(
    r"((?:\d+(?:\.\d+)?%|\d+/\d+|二分之一|三分之一|四分之一|三分之二|四分之三)\s*(?:的)?(?:产权|份额|所有权))"
)


def parse_price_yuan(value: str | int | float | None) -> float | None:
    """Parse Taobao price text (e.g. '¥119.05万', '1.2亿', 524528) into yuan."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        val = float(value)
        return val if val >= 0 else None

    text = str(value).strip().replace(",", "").replace("￥", "").replace("¥", "").replace("元", "")
    if not text:
        return None

    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(亿|万)?", text)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2)
    if unit == "亿":
        return round(num * 100_000_000, 2)
    if unit == "万":
        return round(num * 10_000, 2)
    return round(num, 2)


def normalize_datetime(value: str | int | float | None, reference_year: int | None = None) -> str | None:
    """Normalize timestamps (ms/s epoch) or Chinese date strings into 'YYYY-MM-DD HH:MM:SS'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts <= 0:
            return None
        if ts > 10_000_000_000:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=CN_TZ).strftime("%Y-%m-%d %H:%M:%S")

    text = str(value).strip()
    if not text:
        return None

    if text.isdigit():
        return normalize_datetime(int(text), reference_year=reference_year)

    # YYYY-MM-DD HH:MM(:SS) or YYYY/MM/DD HH:MM(:SS)
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日\sT]+(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if m:
        y, mo, d, h, mi, s = (int(x) if x is not None else 0 for x in m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"

    # MM月DD日 HH:MM
    m = re.search(r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if m:
        year = reference_year or datetime.now(CN_TZ).year
        mo, d, h, mi, s = (int(x) if x is not None else 0 for x in m.groups())
        return f"{year:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"

    return text


def normalize_stage(stage_hint: str = "", title: str = "") -> str:
    combined = f"{stage_hint} {title}"
    for canonical, keywords in STAGE_PATTERNS:
        if any(k in combined for k in keywords):
            return canonical
    return "未知"


def normalize_status(status_hint: str = "", bid_count: int | None = None) -> str:
    s = (status_hint or "").strip().lower()
    if s in {"upcoming", "todo", "0", "即将开始", "即将开拍", "预告中"}:
        return "upcoming"
    if s in {"ongoing", "doing", "1", "正在进行", "进行中", "竞拍中", "拍卖中"}:
        return "ongoing"
    if s in {"sold", "done", "成交", "已成交", "拍卖成交", "变卖成交", "竞价成功"}:
        return "sold"
    if s in {"failed", "failure", "流拍", "已流拍", "无人出价", "流标"}:
        return "failed"
    if s in {"withdrawn", "revoke", "撤回", "已撤回", "撤销", "已下架", "下架"}:
        return "withdrawn"
    if s in {"suspended", "break", "中止", "已中止", "暂缓", "已暂缓", "暂停", "已暂停"}:
        return "suspended"
    if s in {"ended", "2", "已结束", "结束"}:
        if bid_count is not None:
            return "sold" if bid_count > 0 else "failed"
        return "ended"
    if "成交" in s or "竞价成功" in s:
        return "sold"
    if "流拍" in s or "流标" in s:
        return "failed"
    if "撤回" in s or "撤销" in s or "下架" in s:
        return "withdrawn"
    if "中止" in s or "暂缓" in s or "暂停" in s:
        return "suspended"
    if "即将" in s or "预告" in s:
        return "upcoming"
    if "进行" in s or "拍卖中" in s:
        return "ongoing"
    return "unknown"


def extract_district(text: str, city: str = "", explicit_district: str = "") -> str:
    if explicit_district:
        return explicit_district.strip()

    city_clean = city.replace("市", "").strip()
    if city_clean in CITY_DISTRICTS:
        for dist in CITY_DISTRICTS[city_clean]:
            short = dist.rstrip("区县市")
            if dist in text or (len(short) >= 2 and short in text):
                return dist

    m = re.search(r"([\u4e00-\u9fa5]{2,5}(?:区|县|市))", text)
    if m:
        candidate = m.group(1)
        if candidate not in {"福建省", "福州市", "龙岩市", "拍卖市"}:
            return candidate
    return ""


def classify_property_type(title: str, detail_text: str = "", category_hint: str = "") -> str:
    """Classify asset type accurately so non-residential items don't pollute residential stats."""
    t = title or ""
    combined = f"{t} {detail_text[:600]}"

    # Pure parking / storage (exclude residential unit that merely includes an accessory parking spot)
    has_residential_unit = bool(re.search(r"(\d+室|\d+单元|\d+户|住宅|别墅|公寓|商品房|复式|跃层)", t))
    if not has_residential_unit and re.search(r"(车位|车库|泊位|储藏室|杂物间|地下室)", t):
        return "parking"

    if re.search(r"(工业|厂房|仓库|车间|工业用地|生产用房)", t):
        return "industrial"

    if re.search(r"(土地使用权|宗地|林地|农用地|耕地|宅基地)", t) and not has_residential_unit:
        return "land"

    if re.search(r"(商铺|店面|商业用房|写字楼|办公楼|办公室|酒店|宾馆|商场|门面|底商)", t) and "住宅" not in t:
        return "commercial"

    if has_residential_unit or "住宅" in combined or category_hint in {"50025969", "12728", "住宅用房"}:
        return "residential"

    if re.search(r"(商铺|商业|办公|写字楼)", combined):
        return "commercial"

    return "other"


def extract_ownership_share(title: str, detail_text: str = "") -> tuple[bool, str]:
    combined = f"{title} {detail_text[:1000]}"
    m = OWNERSHIP_SHARE_RE.search(combined)
    if m:
        share = m.group(1).strip()
        if not share.startswith("100%"):
            return True, share
    if "部分产权" in combined or "按份共有" in combined:
        return True, "部分产权"
    return False, "100%"


def extract_area_sqm(title: str, detail_text: str = "", explicit_area: float | None = None) -> float | None:
    if explicit_area is not None and 5.0 <= explicit_area <= 50_000.0:
        return round(float(explicit_area), 2)

    combined = f"{title}\n{detail_text}"
    for pattern in BUILDING_AREA_PATTERNS:
        m = pattern.search(combined)
        if m:
            val = float(m.group(1))
            if 5.0 <= val <= 50_000.0:
                return round(val, 2)
    return None


def extract_community_name(title: str, address: str = "") -> str:
    for text in (title, address):
        m = COMMUNITY_RE.search(text or "")
        if m:
            return m.group(1)
    return ""


def normalize_address_for_key(title: str, address: str = "", province: str = "", city: str = "") -> str:
    """Strip auction-round noise so 1st/2nd/sale rounds of the same unit share one key."""
    base = (address or title or "").strip()
    for pat in TITLE_NOISE_PATTERNS:
        base = re.sub(pat, "", base)

    # Full-width to half-width digits/letters & common separators
    trans = str.maketrans(
        "０１２３４５６７８９＃－—（）【】　",
        "0123456789#--()[] ",
    )
    base = base.translate(trans)

    # Normalize common Chinese building unit synonyms
    base = base.replace("号楼", "幢").replace("栋", "幢").replace("#楼", "幢").replace("#", "幢")
    base = re.sub(r"[\s,，。、;；:：'\"“”‘’]+", "", base)

    # Remove redundant province/city prefixes so '福建省福州市鼓楼区...' and '福州市鼓楼区...' match
    for prefix in (
        f"{province}省{city}市",
        f"{province}{city}市",
        f"{province}省{city}",
        f"{province}{city}",
        f"{city}市",
        f"{province}省",
    ):
        if prefix and base.startswith(prefix):
            base = base[len(prefix) :]
            break

    return base.lower()


def build_property_key(province: str, city: str, normalized_address: str, fallback_item_id: str) -> str:
    clean_prov = province.replace("省", "").strip()
    clean_city = city.replace("市", "").strip()
    seed = f"{clean_prov}|{clean_city}|{normalized_address}" if normalized_address else f"id|{fallback_item_id}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]


def extract_risk_tags(title: str, notice_text: str = "", survey_text: str = "") -> tuple[list[str], str]:
    combined = f"{title}\n{notice_text}\n{survey_text}"
    tags: list[str] = []
    max_rank = 0
    rank_map = {"low": 0, "medium": 1, "high": 2}

    for tag, severity, pattern in RISK_RULES:
        if re.search(pattern, combined):
            tags.append(tag)
            if rank_map[severity] > max_rank:
                max_rank = rank_map[severity]

    level = {0: "low", 1: "medium", 2: "high"}[max_rank]
    return tags, level


def parse_property_info(
    *,
    item_id: str,
    title: str,
    province: str = "",
    city: str = "",
    explicit_district: str = "",
    address: str = "",
    category_hint: str = "",
    explicit_area: float | None = None,
    notice_text: str = "",
    survey_text: str = "",
) -> ParsedPropertyInfo:
    detail_text = f"{address}\n{notice_text}\n{survey_text}".strip()
    district = extract_district(f"{title} {address}", city=city, explicit_district=explicit_district)
    property_type = classify_property_type(title, detail_text=detail_text, category_hint=category_hint)
    is_partial, share = extract_ownership_share(title, detail_text=detail_text)
    area_sqm = extract_area_sqm(title, detail_text=detail_text, explicit_area=explicit_area)
    community = extract_community_name(title, address=address)
    norm_addr = normalize_address_for_key(title, address=address, province=province, city=city)
    prop_key = build_property_key(province, city, norm_addr, fallback_item_id=item_id)
    risk_tags, risk_level = extract_risk_tags(title, notice_text=notice_text, survey_text=survey_text)

    if is_partial and "部分产权" not in risk_tags:
        risk_tags.append("部分产权")
        risk_level = "high"

    return ParsedPropertyInfo(
        property_key=prop_key,
        normalized_address=norm_addr,
        community_name=community,
        district=district,
        property_type=property_type,
        is_partial_share=is_partial,
        ownership_share=share,
        area_sqm=area_sqm,
        risk_tags=risk_tags,
        risk_level=risk_level,
    )
