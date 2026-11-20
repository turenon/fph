from __future__ import annotations

import json
import re
from typing import Any

from fph.config import ProviderConfig, RegionConfig
from fph.models import RawAuctionDetail, RawAuctionItem
from fph.normalizer import (
    extract_area_sqm,
    extract_district,
    normalize_datetime,
    normalize_stage,
    normalize_status,
    parse_price_yuan,
)
from fph.providers.base import AuctionProvider, PageResult
from fph.providers.ego_runtime import (
    MARKER_END,
    MARKER_START,
    TASK_SPACE_HELPER_JS,
    CommandRunner,
    EgoBrowserRunner,
    _to_int,
)
from fph.providers.jd_urls import (
    build_jd_item_url,
    build_jd_list_url,
    expand_jd_status_queries,
    resolve_jd_category_id,
)


def _normalize_jd_item_url(url: str, item_id: str = "") -> str:
    u = (url or "").strip()
    if u.startswith("//"):
        return f"https:{u}"
    if u.startswith("/"):
        return f"https://paimai.jd.com{u}"
    if u:
        return u
    return build_jd_item_url(item_id) if item_id else ""


def _extract_jd_item_id_from_url(url: str) -> str:
    m = re.search(r"paimai\.jd\.com/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]paimaiId=(\d+)", url)
    if m:
        return m.group(1)
    return ""


def _parse_optional_area(val: Any) -> float | None:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        num = float(val)
        return round(num, 2) if 5.0 <= num <= 50_000.0 else None
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(val))
    if not m:
        return None
    num = float(m.group(1))
    return round(num, 2) if 5.0 <= num <= 50_000.0 else None


def _build_jd_list_script(task_space: str, url: str, page: int, page_timeout: int) -> str:
    """Build deterministic Node.js script to fetch a JD judicial auction list page via ego-browser."""
    cfg_json = json.dumps(
        {
            "taskSpace": task_space,
            "url": url,
            "page": max(1, page),
            "timeoutMs": max(5, page_timeout) * 1000,
            "markerStart": MARKER_START,
            "markerEnd": MARKER_END,
        },
        ensure_ascii=False,
    )
    return f"""
const CFG = {cfg_json};
{TASK_SPACE_HELPER_JS}

async function waitForListReady(page, timeoutMs) {{
  try {{
    await page.waitForFunction(
      () => {{
        const href = location.href || "";
        if (/passport\\.jd\\.com|plogin\\.m\\.jd\\.com|safe\\.jd\\.com|cfe\\.m\\.jd\\.com|verify/i.test(href)) return true;
        const text = (document.body && document.body.innerText) ? document.body.innerText : "";
        if (document.querySelectorAll('a[href*="paimai.jd.com/"]').length > 0) return true;
        if (/暂无相关标的物|暂无相关招商项目|网络环境异常|扫码登录|账号登录/.test(text)) return true;
        return false;
      }},
      undefined,
      {{ timeout: Math.min(timeoutMs, 15000) }}
    );
  }} catch (_) {{}}
  await page.waitForTimeout(1200);
}}

async function goToTargetPage(page, targetPage, timeoutMs) {{
  for (let hop = 0; hop < targetPage + 2; hop++) {{
    const state = await page.evaluate((p) => {{
      const nav = document.querySelector('nav[aria-label="搜索结果分页"]');
      if (!nav) return {{ hasNav: false, reached: false, hasBtn: false, hasNext: false }};
      const curBtn = nav.querySelector('button[aria-current="page"]');
      const cur = curBtn ? parseInt(curBtn.getAttribute("data-page") || curBtn.innerText || "1", 10) : 1;
      if (cur === p) return {{ hasNav: true, reached: true, cur }};
      const targetBtn = nav.querySelector('button[data-page="' + p + '"]');
      const nextBtn = nav.querySelector('button[data-page="下一页"]');
      return {{
        hasNav: true,
        reached: false,
        cur,
        hasBtn: Boolean(targetBtn && !targetBtn.disabled),
        hasNext: Boolean(nextBtn && !nextBtn.disabled),
      }};
    }}, targetPage);

    if (state.reached) return true;
    if (!state.hasNav || (!state.hasBtn && !state.hasNext)) return false;

    const selector = state.hasBtn
      ? `nav[aria-label="搜索结果分页"] button[data-page="${{targetPage}}"]`
      : 'nav[aria-label="搜索结果分页"] button[data-page="下一页"]';
    await page.click(selector, {{ label: state.hasBtn ? `go to page ${{targetPage}}` : "next result page" }});
    try {{
      await page.waitForFunction(
        (prevCur) => {{
          const nav = document.querySelector('nav[aria-label="搜索结果分页"]');
          if (!nav) return false;
          if (nav.getAttribute("aria-busy") === "true") return false;
          const curBtn = nav.querySelector('button[aria-current="page"]');
          const cur = curBtn ? parseInt(curBtn.getAttribute("data-page") || curBtn.innerText || "1", 10) : 1;
          return cur !== prevCur;
        }},
        state.cur,
        {{ timeout: Math.min(timeoutMs, 12000) }}
      );
    }} catch (_) {{}}
    await page.waitForTimeout(1000);
  }}
  return false;
}}

async function run() {{
  const {{ task, page }} = await getOrCreatePage(CFG.taskSpace);
  const currentTarget = await page.evaluate(() => window.__fphLastTargetUrl || "");
  if (CFG.page <= 1 || currentTarget !== CFG.url) {{
    await page.goto(CFG.url, {{ timeout: CFG.timeoutMs }});
    await waitForListReady(page, CFG.timeoutMs);
    await page.evaluate((u) => {{ window.__fphLastTargetUrl = u; }}, CFG.url);
  }}

  let pageFound = true;
  if (CFG.page > 1) {{
    pageFound = await goToTargetPage(page, CFG.page, CFG.timeoutMs);
  }}

  const result = await page.evaluate((pageOk) => {{
    const href = location.href || "";
    const title = document.title || "";
    const bodyText = (document.body && document.body.innerText) ? document.body.innerText : "";

    const isLoginOrVerify =
      /passport\\.jd\\.com|plogin\\.m\\.jd\\.com|safe\\.jd\\.com|cfe\\.m\\.jd\\.com|verify/i.test(href) ||
      (/扫码登录|账号登录|请登录|安全验证|滑块验证|人机验证/.test(bodyText) && document.querySelectorAll('a[href*="paimai.jd.com/"]').length === 0);

    if (isLoginOrVerify) {{
      return {{
        authRequired: true,
        reason: "检测到京东登录页或安全验证页，请在 ego(lite) 中切入对应 Space 手动登录/验证后重试",
        currentUrl: href,
        pageTitle: title,
      }};
    }}

    if (!pageOk) {{
      return {{
        authRequired: false,
        currentUrl: href,
        pageTitle: title,
        extractor: "empty-page",
        totalPages: null,
        hasNext: false,
        items: [],
      }};
    }}

    const DISPLAY_STATUS_MAP = {{
      1: "",
      3: "已暂停",
      5: "已撤回",
      6: "已暂缓",
      7: "已中止",
      8: "已清痕",
      10: "已下架",
    }};
    const AUCTION_STATUS_MAP = {{
      0: "upcoming",
      1: "ongoing",
      2: "ended",
    }};

    const extractFiberProps = (el) => {{
      const fiberKey = Object.keys(el).find((k) => k.startsWith("__reactFiber$") || k.startsWith("__reactInternalInstance$"));
      if (!fiberKey) return null;
      let cur = el[fiberKey];
      for (let i = 0; i < 8 && cur; i += 1, cur = cur.return) {{
        const props = cur.memoizedProps;
        if (props && props.item && props.item.data && props.item.data.paimaiId) {{
          return props;
        }}
      }}
      return null;
    }};

    let items = [];
    let extractor = "none";
    const articles = Array.from(document.querySelectorAll("article"));

    // Strategy 1: React fiber props on search result cards
    for (const art of articles) {{
      const props = extractFiberProps(art);
      if (!props) continue;
      const d = props.item.data || {{}};
      const rt = props.realtimeInfo || {{}};
      const paimaiId = String(d.paimaiId || "");
      if (!paimaiId) continue;

      const badgeEl = art.querySelector('[class*="_titleLabel_"]');
      const badgeText = badgeEl ? (badgeEl.innerText || "").trim() : "";
      const pillEl = art.querySelector('[class*="_statusPill_"]');
      const pillText = pillEl ? (pillEl.innerText || "").trim() : "";

      const dispCode = rt.displayStatus ?? d.displayStatus;
      const aucCode = rt.auctionStatus ?? d.paimaiStatus;
      const mappedStatus =
        (dispCode !== undefined && DISPLAY_STATUS_MAP[dispCode]) ||
        pillText ||
        (aucCode !== undefined && AUCTION_STATUS_MAP[aucCode]) ||
        "";

      const ha = (d.houseAttributes && typeof d.houseAttributes === "object") ? d.houseAttributes : {{}};
      const locText = Array.isArray(d.location) ? d.location.join(" ") : String(d.location || "");

      items.push({{
        id: paimaiId,
        title: String(d.title || "").trim(),
        url: `https://paimai.jd.com/${{paimaiId}}`,
        status: mappedStatus,
        stage: badgeText || String(d.title || ""),
        initialPrice: rt.startPrice ?? d.startPrice ?? null,
        currentPrice: rt.currentPrice ?? d.currentPrice ?? null,
        consultPrice: d.assessmentPrice ?? d.marketPrice ?? null,
        depositPrice: null,
        bidCount: rt.bidCount ?? d.bidCount ?? null,
        applyCount: null,
        watchCount: rt.accessNumber ?? d.accessNumber ?? d.accessNum ?? d.onlookersNum ?? null,
        start: rt.startTime ?? d.startTime ?? null,
        end: rt.endTime ?? d.endTime ?? null,
        areaSqm: ha.houseArea ?? null,
        communityName: ha.plotName ? String(ha.plotName).trim() : "",
        location: locText,
        raw: {{ data: d, realtimeInfo: rt }},
      }});
    }}
    if (items.length > 0) {{
      extractor = "react-fiber";
    }}

    // Strategy 2: Generic DOM card fallback
    if (items.length === 0) {{
      const links = Array.from(document.querySelectorAll('a[href*="paimai.jd.com/"]'));
      const seen = new Set();
      for (const a of links) {{
        const hrefAttr = a.getAttribute("href") || a.href || "";
        const idMatch = hrefAttr.match(/paimai\\.jd\\.com\\/(\\d+)/);
        if (!idMatch) continue;
        const id = idMatch[1];
        if (seen.has(id)) continue;
        seen.add(id);

        const card = a.closest("article, li, [class*='_card_'], [class*='card']") || a.parentElement;
        const cardText = (card && card.innerText) ? card.innerText : (a.innerText || "");
        const titleEl = card ? card.querySelector('h3, [class*="_title_"], [class*="title"]') : null;
        const imgEl = card ? card.querySelector("img[alt]") : null;
        const badgeEl = card ? card.querySelector('[class*="_titleLabel_"]') : null;
        const badgeText = badgeEl ? (badgeEl.innerText || "").trim() : "";

        let itemTitle = imgEl && imgEl.getAttribute("alt") ? imgEl.getAttribute("alt").trim() : "";
        if (!itemTitle && titleEl) {{
          itemTitle = (titleEl.innerText || "").trim();
          if (badgeText && itemTitle.startsWith(badgeText)) {{
            itemTitle = itemTitle.slice(badgeText.length).trim();
          }}
        }}
        if (!itemTitle) continue;

        const pick = (re) => {{
          const m = cardText.match(re);
          return m ? m[1] : null;
        }};
        const pillEl = card ? card.querySelector('[class*="_statusPill_"]') : null;
        const priceLabelEl = card ? card.querySelector('[class*="_priceLabel_"]') : null;
        const priceLabel = priceLabelEl ? (priceLabelEl.innerText || "") : "";
        const priceValEl = card ? card.querySelector('[class*="_currentPriceValue_"]') : null;
        const priceVal = priceValEl ? (priceValEl.innerText || "").trim() : null;
        const assessEl = card ? card.querySelector('[class*="_assessValue_"]') : null;
        const assessVal = assessEl ? (assessEl.innerText || "").trim() : null;
        const infoItems = card ? Array.from(card.querySelectorAll('[class*="_infoItem_"]')).map((el) => (el.innerText || "").replace(/^｜/, "").trim()) : [];
        const timeEl = card ? card.querySelector('[class*="_statusTimeText_"]') : null;
        const timeText = timeEl ? (timeEl.innerText || "").trim() : "";

        let initialPrice = null;
        let currentPrice = null;
        if (priceVal) {{
          if (/起拍|变卖/.test(priceLabel)) initialPrice = priceVal;
          else currentPrice = priceVal;
        }}
        const areaMatch = cardText.match(/([0-9]+(?:\\.[0-9]+)?)\\s*(?:㎡|m²|平方米|平米)/i);

        items.push({{
          id,
          title: itemTitle,
          url: hrefAttr,
          status: (pillEl ? (pillEl.innerText || "").trim() : "") || pick(/(正在进行|进行中|即将开始|即将开拍|预告中|已成交|已流拍|已结束|已撤回|已中止|已暂缓|已下架)/) || "",
          stage: badgeText || pick(/(一拍|二拍|变卖|重新拍卖)/) || itemTitle,
          initialPrice,
          currentPrice,
          consultPrice: assessVal,
          depositPrice: null,
          bidCount: pick(/(\\d+)\\s*次出价/),
          applyCount: null,
          watchCount: pick(/(\\d+)\\s*人围观/),
          start: /开始/.test(timeText) ? timeText.replace(/开始.*$/, "").trim() : null,
          end: /结束/.test(timeText) ? timeText.replace(/结束.*$/, "").trim() : null,
          areaSqm: areaMatch ? areaMatch[1] : null,
          communityName: "",
          location: infoItems.join(" "),
          raw: {{ cardText: cardText.slice(0, 600) }},
        }});
      }}
      if (items.length > 0) {{
        extractor = "dom-cards";
      }}
    }}

    // Pagination detection
    let totalPages = null;
    const nav = document.querySelector('nav[aria-label="搜索结果分页"]');
    let hasNext = false;
    if (nav) {{
      const pageNums = Array.from(nav.querySelectorAll("button[data-page]"))
        .map((b) => parseInt(b.getAttribute("data-page") || "", 10))
        .filter((n) => Number.isFinite(n) && n > 0);
      if (pageNums.length > 0) {{
        totalPages = Math.max(...pageNums);
      }}
      const nextBtn = nav.querySelector('button[data-page="下一页"]');
      hasNext = Boolean(nextBtn && !nextBtn.disabled);
    }}

    return {{
      authRequired: false,
      currentUrl: href,
      pageTitle: title,
      extractor,
      totalPages,
      hasNext,
      items,
    }};
  }}, pageFound);

  if (result && result.authRequired) {{
    try {{
      await task.handOff();
    }} catch (_) {{}}
  }}

  console.log(CFG.markerStart + JSON.stringify(result) + CFG.markerEnd);
}}

run().catch((err) => {{
  console.log(CFG.markerStart + JSON.stringify({{ error: String(err && err.stack ? err.stack : err) }}) + CFG.markerEnd);
  process.exit(1);
}});
""".strip()


def _build_jd_detail_script(task_space: str, url: str, page_timeout: int) -> str:
    """Build deterministic Node.js script to extract JD judicial auction detail & announcement."""
    cfg_json = json.dumps(
        {
            "taskSpace": task_space,
            "url": url,
            "timeoutMs": max(5, page_timeout) * 1000,
            "markerStart": MARKER_START,
            "markerEnd": MARKER_END,
        },
        ensure_ascii=False,
    )
    return f"""
const CFG = {cfg_json};
{TASK_SPACE_HELPER_JS}

async function run() {{
  const {{ task, page }} = await getOrCreatePage(CFG.taskSpace);
  await page.goto(CFG.url, {{ timeout: CFG.timeoutMs }});
  await page.waitForTimeout(2000);
  await page.evaluate(() => window.scrollBy(0, 1000));
  await page.waitForTimeout(1000);

  const result = await page.evaluate(() => {{
    const href = location.href || "";
    const title = document.title || "";
    const bodyText = (document.body && document.body.innerText) ? document.body.innerText : "";

    const isLoginOrVerify =
      /passport\\.jd\\.com|plogin\\.m\\.jd\\.com|safe\\.jd\\.com|cfe\\.m\\.jd\\.com|verify/i.test(href) ||
      (/扫码登录|账号登录|请登录|安全验证|滑块验证|人机验证/.test(bodyText) && bodyText.length < 1500);

    if (isLoginOrVerify) {{
      return {{
        authRequired: true,
        reason: "京东详情页触发登录或安全验证，请在 ego(lite) 中切入对应 Space 手动处理后重试",
        currentUrl: href,
      }};
    }}

    const pick = (re) => {{
      const m = bodyText.match(re);
      return m ? m[1].trim() : null;
    }};

    const h1 = document.querySelector('h1, .pm-name, .item-title, [class*="title"] h1');
    const itemTitle = h1 ? (h1.innerText || "").trim() : title.replace(/-京东拍卖.*$/, "").trim();

    const noticeEl = document.querySelector('#J_NoticeDetail, .notice-detail, [id*="notice" i], [class*="notice" i], [class*="announcement" i]');
    const surveyEl = document.querySelector('#J_ItemDesc, .item-desc, [id*="intro" i], [class*="introduce" i], [class*="survey" i], [class*="detail-content" i]');

    const noticeText = noticeEl ? (noticeEl.innerText || "").trim() : bodyText.slice(0, 6000);
    const surveyText = surveyEl ? (surveyEl.innerText || "").trim() : "";

    const courtEl = document.querySelector('[class*="court" i], [class*="agency" i], [class*="organ" i], .unit-txt');
    const courtName = courtEl
      ? (courtEl.innerText || "").trim()
      : (pick(/(?:处置单位|送拍机构|处置法院|拍品所属)[：:\\s]*([^\\n]{{2,36}})/) || "");

    const address = pick(/(?:标的物所在地|标的物地址|房产坐落|坐落|地址)[：:\\s]*([^\\n]{{4,80}})/) || "";
    const areaSqm = pick(/建筑面积[：:约为\\s]*([0-9]+(?:\\.[0-9]+)?)\\s*(?:平方米|平米|㎡|m²|m2)/i);

    return {{
      authRequired: false,
      currentUrl: href,
      title: itemTitle,
      status: pick(/(竞价成功|拍卖已成交|已成交|本场已流拍|已流拍|流标|正在进行|进行中|拍卖中|即将开始|即将开拍|预告中|已撤回|已下架|已中止|已暂缓|已暂停|已结束)/) || "",
      currentPrice: pick(/(?:成交价|当前价|变卖价)[：:\\s]*[¥￥]?([0-9.,]+\\s*[万亿]?)/),
      initialPrice: pick(/起\\s*拍\\s*价[：:\\s]*[¥￥]?([0-9.,]+\\s*[万亿]?)/),
      consultPrice: pick(/(?:评\\s*估\\s*价|市\\s*场\\s*价)[：:\\s]*[¥￥]?([0-9.,]+\\s*[万亿]?)/),
      depositPrice: pick(/保\\s*证\\s*金[：:\\s]*[¥￥]?([0-9.,]+\\s*[万亿]?)/),
      bidCount: pick(/(\\d+)\\s*次出价/) || pick(/出价记录\\s*[（(](\\d+)[）)]/),
      applyCount: pick(/(\\d+)\\s*人报名/),
      watchCount: pick(/(\\d+)\\s*(?:次|人)?围观/),
      courtName,
      address,
      areaSqm,
      noticeText: noticeText.slice(0, 12000),
      surveyText: surveyText.slice(0, 8000),
    }};
  }});

  if (result && result.authRequired) {{
    try {{
      await task.handOff();
    }} catch (_) {{}}
  }}

  console.log(CFG.markerStart + JSON.stringify(result) + CFG.markerEnd);
}}

run().catch((err) => {{
  console.log(CFG.markerStart + JSON.stringify({{ error: String(err && err.stack ? err.stack : err) }}) + CFG.markerEnd);
  process.exit(1);
}});
""".strip()


class JdEgoProvider(AuctionProvider):
    """JD judicial auction provider backed by local ego(lite) / ego-browser."""

    platform = "jd"

    def __init__(
        self,
        config: ProviderConfig,
        runner: CommandRunner | None = None,
    ) -> None:
        self.config = config
        self._ego = EgoBrowserRunner(config, runner=runner)

    def check_ready(self) -> tuple[bool, str]:
        return self._ego.check_ready()

    def expand_status_queries(self, status_scope: str) -> list[tuple[str, str]]:
        return expand_jd_status_queries(status_scope)

    def preview_list_url(self, region: RegionConfig, st_param: str, page: int = 1) -> str:
        return build_jd_list_url(region, st_param=st_param, page=page)

    def _run_ego_script(self, script: str) -> dict[str, Any]:
        return self._ego.run_script(script)

    def fetch_list_page(
        self,
        region: RegionConfig,
        status_scope: str,
        page: int,
    ) -> PageResult:
        url = build_jd_list_url(region, st_param=status_scope, page=page)
        script = _build_jd_list_script(
            task_space=self.config.task_space,
            url=url,
            page=page,
            page_timeout=self.config.page_timeout_seconds,
        )
        data = self._run_ego_script(script)

        items: list[RawAuctionItem] = []
        cat_hint = resolve_jd_category_id(region)
        for raw in data.get("items") or []:
            item_url = _normalize_jd_item_url(str(raw.get("url") or ""))
            item_id = str(raw.get("id") or _extract_jd_item_id_from_url(item_url)).strip()
            title = str(raw.get("title") or "").strip()
            if not item_id or not title:
                continue

            bid_count = _to_int(raw.get("bidCount"))
            raw_status = str(raw.get("status") or "").strip()
            if not raw_status:
                if status_scope == "102":
                    raw_status = "upcoming"
                elif status_scope == "101":
                    raw_status = "ongoing"
                elif status_scope == "105":
                    raw_status = "ended"

            loc_text = str(raw.get("location") or "").strip()
            district = extract_district(f"{loc_text} {title}", city=region.city)
            area_sqm = _parse_optional_area(raw.get("areaSqm"))

            items.append(
                RawAuctionItem(
                    platform=self.platform,
                    item_id=item_id,
                    url=item_url or build_jd_item_url(item_id),
                    title=title,
                    province=region.province,
                    city=region.city,
                    district=district,
                    stage=normalize_stage(str(raw.get("stage") or ""), title),
                    status=normalize_status(raw_status, bid_count=bid_count),
                    initial_price=parse_price_yuan(raw.get("initialPrice")),
                    current_price=parse_price_yuan(raw.get("currentPrice")),
                    consult_price=parse_price_yuan(raw.get("consultPrice")),
                    deposit_price=parse_price_yuan(raw.get("depositPrice")),
                    bid_count=bid_count,
                    apply_count=_to_int(raw.get("applyCount")),
                    watch_count=_to_int(raw.get("watchCount")),
                    start_time=normalize_datetime(raw.get("start")),
                    end_time=normalize_datetime(raw.get("end")),
                    court_name=str(raw.get("courtName") or "").strip(),
                    category_hint=cat_hint,
                    raw_payload=raw.get("raw") if isinstance(raw.get("raw"), dict) else raw,
                    community_name=str(raw.get("communityName") or "").strip(),
                    area_sqm=area_sqm,
                )
            )

        total_pages = _to_int(data.get("totalPages"))
        has_next = bool(data.get("hasNext"))
        if total_pages is not None and page >= total_pages:
            has_next = False

        return PageResult(
            items=items,
            page=page,
            total_pages=total_pages,
            has_next=has_next,
            source_url=str(data.get("currentUrl") or url),
            extractor=str(data.get("extractor") or "unknown"),
        )

    def fetch_detail(self, item_id: str, url: str) -> RawAuctionDetail:
        target_url = _normalize_jd_item_url(url, item_id=item_id) or build_jd_item_url(item_id)
        script = _build_jd_detail_script(
            task_space=self.config.task_space,
            url=target_url,
            page_timeout=self.config.page_timeout_seconds,
        )
        data = self._run_ego_script(script)
        bid_count = _to_int(data.get("bidCount"))
        title = str(data.get("title") or "").strip()
        notice_text = str(data.get("noticeText") or "").strip()
        survey_text = str(data.get("surveyText") or "").strip()
        area_sqm = _parse_optional_area(data.get("areaSqm")) or extract_area_sqm(
            title,
            f"{notice_text}\n{survey_text}",
        )

        return RawAuctionDetail(
            platform=self.platform,
            item_id=item_id,
            url=str(data.get("currentUrl") or target_url),
            title=title,
            status=normalize_status(str(data.get("status") or ""), bid_count=bid_count),
            current_price=parse_price_yuan(data.get("currentPrice")),
            initial_price=parse_price_yuan(data.get("initialPrice")),
            consult_price=parse_price_yuan(data.get("consultPrice")),
            deposit_price=parse_price_yuan(data.get("depositPrice")),
            bid_count=bid_count,
            apply_count=_to_int(data.get("applyCount")),
            watch_count=_to_int(data.get("watchCount")),
            court_name=str(data.get("courtName") or "").strip(),
            address=str(data.get("address") or "").strip(),
            area_sqm=area_sqm,
            notice_text=notice_text,
            survey_text=survey_text,
            raw_payload=data,
        )
