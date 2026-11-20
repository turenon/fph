from __future__ import annotations

import json
import re
from typing import Any

from fph.config import ProviderConfig, RegionConfig
from fph.models import RawAuctionDetail, RawAuctionItem
from fph.normalizer import normalize_datetime, normalize_stage, normalize_status, parse_price_yuan
from fph.providers.base import AuctionProvider, PageResult
from fph.providers.ego_runtime import (
    MARKER_END,
    MARKER_START,
    TASK_SPACE_HELPER_JS,
    CommandRunner,
    EgoBrowserRunner,
    _to_int,
)
from fph.providers.taobao_urls import build_list_url, expand_status_queries as _expand_taobao_status_queries


def _normalize_item_url(url: str) -> str:
    u = (url or "").strip()
    if u.startswith("//"):
        return f"https:{u}"
    if u.startswith("/"):
        return f"https://sf.taobao.com{u}"
    return u


def _extract_item_id_from_url(url: str) -> str:
    m = re.search(r"sf_item/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=(\d+)", url)
    if m:
        return m.group(1)
    return ""


def _build_list_script(task_space: str, url: str, page_timeout: int) -> str:
    """Build deterministic Node.js script executed by `ego-browser nodejs`."""
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

  const result = await page.evaluate(() => {{
    const href = location.href || '';
    const title = document.title || '';
    const bodyText = (document.body && document.body.innerText) ? document.body.innerText : '';

    const isLoginOrVerify =
      /login\\.taobao\\.com|passport\\.taobao\\.com|sec\\.taobao\\.com|punish|baxia|verify/i.test(href) ||
      (/请登录|扫码登录|安全验证|滑块验证|人机验证|亲，请登录/.test(bodyText) && !document.querySelector('#sf-item-list-data') && document.querySelectorAll('a[href*="sf_item/"]').length === 0);

    if (isLoginOrVerify) {{
      return {{
        authRequired: true,
        reason: '检测到登录页或安全验证页，请在 ego(lite) 中切入对应 Space 手动登录后重试',
        currentUrl: href,
        pageTitle: title,
      }};
    }}

    // Strategy 1: Embedded JSON block (#sf-item-list-data)
    const jsonEl = document.querySelector('#sf-item-list-data');
    let items = [];
    let extractor = 'none';
    if (jsonEl && jsonEl.textContent && jsonEl.textContent.trim()) {{
      try {{
        const parsed = JSON.parse(jsonEl.textContent.trim());
        const list = Array.isArray(parsed) ? parsed : (Array.isArray(parsed.data) ? parsed.data : []);
        if (list.length > 0) {{
          extractor = 'sf-item-list-data';
          items = list.map((it) => ({{
            id: String(it.id || it.itemId || ''),
            title: String(it.title || ''),
            url: String(it.itemUrl || it.url || ''),
            status: String(it.status || ''),
            stage: String(it.stage || it.title || ''),
            initialPrice: it.initialPrice ?? null,
            currentPrice: it.currentPrice ?? null,
            consultPrice: it.consultPrice ?? it.marketPrice ?? null,
            depositPrice: it.depositPrice ?? null,
            bidCount: it.bidCount ?? null,
            applyCount: it.applyCount ?? null,
            watchCount: it.viewerCount ?? it.watchCount ?? null,
            start: it.start ?? null,
            end: it.end ?? null,
            courtName: String(it.courtName || it.orgName || ''),
            raw: it,
          }}));
        }}
      }} catch (e) {{
        // Fallback to DOM extraction
      }}
    }}

    // Strategy 2: Generic DOM card fallback
    if (items.length === 0) {{
      const links = Array.from(document.querySelectorAll('a[href*="sf_item/"]'));
      const seen = new Set();
      for (const a of links) {{
        const hrefAttr = a.getAttribute('href') || a.href || '';
        const idMatch = hrefAttr.match(/sf_item\\/(\\d+)/);
        if (!idMatch) continue;
        const id = idMatch[1];
        if (seen.has(id)) continue;
        seen.add(id);

        const card = a.closest('li, .sf-item, .item-card, [class*="item"]') || a.parentElement;
        const cardText = (card && card.innerText) ? card.innerText : (a.innerText || '');
        const titleEl = card ? card.querySelector('.title, .item-title, p.title, [class*="title"]') : null;
        const itemTitle = (titleEl && titleEl.innerText ? titleEl.innerText : (a.getAttribute('title') || a.innerText || '')).trim();

        const pick = (re) => {{
          const m = cardText.match(re);
          return m ? m[1] : null;
        }};

        items.push({{
          id,
          title: itemTitle,
          url: hrefAttr,
          status: pick(/(正在进行|即将开始|已成交|已流拍|已结束|已撤回|已中止|成交|流拍)/) || '',
          stage: pick(/(一拍|二拍|变卖|重新拍卖)/) || itemTitle,
          initialPrice: pick(/起拍价\\s*[¥￥]?([0-9.,]+\\s*[万亿]?)/) || pick(/变卖价\\s*[¥￥]?([0-9.,]+\\s*[万亿]?)/),
          currentPrice: pick(/(?:当前价|成交价)\\s*[¥￥]?([0-9.,]+\\s*[万亿]?)/),
          consultPrice: pick(/(?:评估价|市场价)\\s*[¥￥]?([0-9.,]+\\s*[万亿]?)/),
          depositPrice: pick(/保证金\\s*[¥￥]?([0-9.,]+\\s*[万亿]?)/),
          bidCount: pick(/(\\d+)\\s*次出价/),
          applyCount: pick(/(\\d+)\\s*人报名/),
          watchCount: pick(/(\\d+)\\s*次围观/),
          start: pick(/开拍时间\\s*([0-9年月日:\\s\\-\\/]+)/),
          end: pick(/(?:预计|结束)\\s*([0-9年月日:\\s\\-\\/]+)\\s*(?:结束)?/),
          courtName: '',
          raw: {{ cardText: cardText.slice(0, 600) }},
        }});
      }}
      if (items.length > 0) {{
        extractor = 'dom-cards';
      }}
    }}

    // Pagination detection
    let totalPages = null;
    const pageTotalEl = document.querySelector('.page-total, .total-page, [class*="page-total"]');
    if (pageTotalEl) {{
      const m = (pageTotalEl.innerText || '').match(/(\\d+)/);
      if (m) totalPages = parseInt(m[1], 10);
    }}
    const nextBtn = document.querySelector('a.next:not(.disabled), a[class*="next"]:not([class*="disabled"])');
    const hasNext = Boolean(nextBtn && !nextBtn.classList.contains('disabled'));

    return {{
      authRequired: false,
      currentUrl: href,
      pageTitle: title,
      extractor,
      totalPages,
      hasNext,
      items,
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


def _build_detail_script(task_space: str, url: str, page_timeout: int) -> str:
    """Build deterministic Node.js script to extract detail & notice text."""
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
  await page.evaluate(() => window.scrollBy(0, 800));
  await page.waitForTimeout(1000);

  const result = await page.evaluate(() => {{
    const href = location.href || '';
    const title = document.title || '';
    const bodyText = (document.body && document.body.innerText) ? document.body.innerText : '';

    const isLoginOrVerify =
      /login\\.taobao\\.com|passport\\.taobao\\.com|sec\\.taobao\\.com|punish|baxia|verify/i.test(href) ||
      (/请登录|扫码登录|安全验证|滑块验证|人机验证/.test(bodyText) && bodyText.length < 1500);

    if (isLoginOrVerify) {{
      return {{
        authRequired: true,
        reason: '详情页触发登录或安全验证，请在 ego(lite) 中切入对应 Space 手动处理后重试',
        currentUrl: href,
      }};
    }}

    const pick = (re) => {{
      const m = bodyText.match(re);
      return m ? m[1].trim() : null;
    }};

    const h1 = document.querySelector('h1, .pm-main-l h1, [class*="title"] h1');
    const itemTitle = h1 ? (h1.innerText || '').trim() : title.replace(/-阿里拍卖.*$/, '').trim();

    const noticeEl = document.querySelector('#J_NoticeDetail, .notice-detail, [id*="Notice"], [class*="notice-content"]');
    const surveyEl = document.querySelector('#J_desc, #J_ItemDesc, .detail-common-text, [class*="item-desc"], [class*="survey"]');

    const noticeText = noticeEl ? (noticeEl.innerText || '').trim() : bodyText.slice(0, 6000);
    const surveyText = surveyEl ? (surveyEl.innerText || '').trim() : '';

    const courtEl = document.querySelector('.unit-txt, .court-name, [class*="court"], [class*="org-name"]');
    const courtName = courtEl ? (courtEl.innerText || '').trim() : (pick(/处置单位[：:\\s]*([^\\n]{{2,30}})/) || '');

    const address = pick(/(?:标的物所在地|房产坐落|坐落|地址)[：:\\s]*([^\\n]{{4,80}})/) || '';

    return {{
      authRequired: false,
      currentUrl: href,
      title: itemTitle,
      status: pick(/(拍卖已成交|已成交|本场已流拍|已流拍|正在进行|即将开始|已撤回|已中止|已结束)/) || '',
      currentPrice: pick(/(?:成交价|当前价|变卖价)[：:\\s]*[¥￥]?([0-9.,]+\\s*[万亿]?)/),
      initialPrice: pick(/起\\s*拍\\s*价[：:\\s]*[¥￥]?([0-9.,]+\\s*[万亿]?)/),
      consultPrice: pick(/(?:评\\s*估\\s*价|市\\s*场\\s*价)[：:\\s]*[¥￥]?([0-9.,]+\\s*[万亿]?)/),
      depositPrice: pick(/保\\s*证\\s*金[：:\\s]*[¥￥]?([0-9.,]+\\s*[万亿]?)/),
      bidCount: pick(/(\\d+)\\s*次出价/) || pick(/出价记录\\s*[（(](\\d+)[）)]/),
      applyCount: pick(/(\\d+)\\s*人报名/),
      watchCount: pick(/(\\d+)\\s*次围观/),
      courtName,
      address,
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


class TaobaoEgoProvider(AuctionProvider):
    """Taobao judicial auction provider backed by local ego(lite) / ego-browser."""

    platform = "taobao"

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
        return _expand_taobao_status_queries(status_scope)

    def preview_list_url(self, region: RegionConfig, st_param: str, page: int = 1) -> str:
        return build_list_url(region, st_param=st_param, page=page)

    def _run_ego_script(self, script: str) -> dict[str, Any]:
        return self._ego.run_script(script)

    def fetch_list_page(
        self,
        region: RegionConfig,
        status_scope: str,
        page: int,
    ) -> PageResult:
        # status_scope here may already be a raw st_param ('0','1','2','-1') or logical name
        st_param = status_scope if status_scope in {"-1", "0", "1", "2", "3", "4"} else "1"
        url = build_list_url(region, st_param=st_param, page=page)
        script = _build_list_script(
            task_space=self.config.task_space,
            url=url,
            page_timeout=self.config.page_timeout_seconds,
        )
        data = self._run_ego_script(script)

        items: list[RawAuctionItem] = []
        for raw in data.get("items") or []:
            item_url = _normalize_item_url(str(raw.get("url") or ""))
            item_id = str(raw.get("id") or _extract_item_id_from_url(item_url)).strip()
            title = str(raw.get("title") or "").strip()
            if not item_id or not title:
                continue

            bid_count = _to_int(raw.get("bidCount"))
            raw_status = str(raw.get("status") or "")
            if not raw_status:
                if st_param == "0":
                    raw_status = "upcoming"
                elif st_param == "1":
                    raw_status = "ongoing"
                elif st_param == "2":
                    raw_status = "ended"

            items.append(
                RawAuctionItem(
                    platform=self.platform,
                    item_id=item_id,
                    url=item_url or f"https://sf-item.taobao.com/sf_item/{item_id}.htm",
                    title=title,
                    province=region.province,
                    city=region.city,
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
                    category_hint=region.category,
                    raw_payload=raw.get("raw") if isinstance(raw.get("raw"), dict) else raw,
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
        target_url = _normalize_item_url(url) or f"https://sf-item.taobao.com/sf_item/{item_id}.htm"
        script = _build_detail_script(
            task_space=self.config.task_space,
            url=target_url,
            page_timeout=self.config.page_timeout_seconds,
        )
        data = self._run_ego_script(script)
        bid_count = _to_int(data.get("bidCount"))
        return RawAuctionDetail(
            platform=self.platform,
            item_id=item_id,
            url=str(data.get("currentUrl") or target_url),
            title=str(data.get("title") or "").strip(),
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
            notice_text=str(data.get("noticeText") or "").strip(),
            survey_text=str(data.get("surveyText") or "").strip(),
            raw_payload=data,
        )
