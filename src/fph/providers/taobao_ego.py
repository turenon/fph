from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import urlsplit

from fph.config import ProviderConfig, RegionConfig
from fph.models import RawAuctionDetail, RawAuctionItem
from fph.normalizer import normalize_datetime, normalize_stage, normalize_status, parse_price_yuan
from fph.providers.base import AuctionProvider, AuthRequiredError, PageResult, ProviderError
from fph.providers.ego_runtime import (
    MARKER_END,
    MARKER_START,
    TASK_SPACE_HELPER_JS,
    CommandRunner,
    EgoBrowserRunner,
    _to_int,
)
from fph.providers.taobao_urls import build_list_url, expand_status_queries as _expand_taobao_status_queries

_CAPTCHA_RISK_RE = re.compile(
    r"拖动下方滑块|请按住滑块|滑动验证|滑块验证|完成验证|通过验证以确保正常访问|baxia|punish|x5sec",
    re.IGNORECASE,
)

_STATUS_SCOPE_FALLBACK: dict[str, str] = {
    "ongoing": "0",
    "upcoming": "1",
    "ended": "2",
    "all": "-1",
}


def _clean_text(value: Any) -> str:
    return html.unescape(str(value or "")).strip()


def _normalize_item_url(url: str) -> str:
    u = (url or "").strip()
    if u.startswith("//"):
        u = f"https:{u}"
    elif u.startswith("/"):
        u = f"https://sf.taobao.com{u}"
    if not u:
        return ""
    parsed = urlsplit(u)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        host == "taobao.com"
        or host.endswith(".taobao.com")
        or host == "tmall.com"
        or host.endswith(".tmall.com")
    ):
        return ""
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

    const captchaRe = /拖动下方滑块|请按住滑块|滑动验证|滑块验证|完成验证|通过验证以确保正常访问|baxia|punish|x5sec/i;
    const isSliderOrRisk =
      /login\\.taobao\\.com|passport\\.taobao\\.com|sec\\.taobao\\.com|punish|baxia|x5sec|verify/i.test(href) ||
      captchaRe.test(bodyText) ||
      captchaRe.test(title) ||
      Boolean(
        document.querySelector(
          '#baxia-dialog-content, .baxia-dialog, #nc_1_n1z, .nc_iconfont, iframe[src*="punish"], iframe[src*="baxia"], iframe[src*="x5sec"]'
        )
      );

    const hasNoResultBanner = /没有您要找的标的物/.test(bodyText);
    const jsonEl = document.querySelector('#sf-item-list-data');
    const hasEmbeddedDataEl = Boolean(jsonEl);

    const isLoginPage =
      /请登录|扫码登录|安全验证|人机验证|亲，请登录/.test(bodyText) &&
      !hasEmbeddedDataEl &&
      !hasNoResultBanner &&
      document.querySelectorAll('a[href*="sf_item/"]').length === 0;

    if (isSliderOrRisk || isLoginPage) {{
      return {{
        authRequired: true,
        reason: '检测到登录页或滑块风控验证页，请在 ego(lite) 中切入对应 Space 手动处理后重试',
        currentUrl: href,
        pageTitle: title,
      }};
    }}

    let items = [];
    let extractor = 'none';

    if (hasNoResultBanner) {{
      extractor = 'empty-result';
    }} else if (hasEmbeddedDataEl) {{
      // Strategy 1: Embedded JSON block (#sf-item-list-data).
      // Even when the parsed list is empty, NEVER fall through to Strategy 2 (which scrapes recommended items).
      extractor = 'sf-item-list-data';
      if (jsonEl.textContent && jsonEl.textContent.trim()) {{
        try {{
          const parsed = JSON.parse(jsonEl.textContent.trim());
          const list = Array.isArray(parsed) ? parsed : (Array.isArray(parsed.data) ? parsed.data : []);
          items = list.map((it) => ({{
            id: String(it.id || it.itemId || ''),
            title: String(it.title || ''),
            url: String(it.itemUrl || it.url || ''),
            status: String(it.status || ''),
            stage: String(it.stage || (it.sellOff ? '变卖' : '') || it.title || ''),
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
            address: String(it.address || ''),
            raw: it,
          }}));
        }} catch (e) {{
          extractor = 'sf-item-list-data-error';
        }}
      }}
    }} else {{
      // Strategy 2: Generic DOM card fallback (only when #sf-item-list-data is absent and page has results)
      const links = Array.from(document.querySelectorAll('a[href*="sf_item/"]'));
      const seen = new Set();
      for (const a of links) {{
        if (a.closest && a.closest('[class*="recommend"], [id*="recommend"], .sf-recommend')) continue;
        const hrefAttr = a.getAttribute('href') || a.href || '';
        const idMatch = hrefAttr.match(/sf_item\\/(\\d+)/);
        if (!idMatch) continue;
        const id = idMatch[1];
        if (seen.has(id)) continue;
        seen.add(id);

        const card = (a.closest && a.closest('li, .sf-item, .item-card, [class*="item"]')) || a.parentElement;
        const cardText = (card && card.innerText) ? card.innerText : (a.innerText || '');
        const titleEl = card && card.querySelector ? card.querySelector('.title, .item-title, p.title, [class*="title"]') : null;
        const itemTitle = (titleEl && titleEl.innerText ? titleEl.innerText : ((a.getAttribute && a.getAttribute('title')) || a.innerText || '')).trim();

        const pick = (re) => {{
          const m = cardText.match(re);
          return m ? m[1] : null;
        }};

        items.push({{
          id,
          title: itemTitle,
          url: hrefAttr,
          status: pick(/(正在进行|即将开始|已成交|已流拍|已结束|已撤回|已撤拍|撤拍|已中止|成交|流拍)/) || '',
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
          address: '',
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
    const hasNext = items.length > 0 && Boolean(nextBtn && !(nextBtn.classList && nextBtn.classList.contains('disabled')));

    return {{
      authRequired: false,
      currentUrl: href,
      pageTitle: title,
      bodyText: bodyText.slice(0, 1200),
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
  await page.waitForTimeout(1500);
  await page.evaluate(() => {{
    const el = document.querySelector('#J_NoticeDetail, .notice-detail, [class*="notice-content"]');
    if (el && el.scrollIntoView) {{
      el.scrollIntoView({{ block: 'center' }});
    }} else {{
      window.scrollBy(0, 1200);
    }}
    const noticeTab = Array.from(document.querySelectorAll('li a, .tab-nav a, [role="tab"]')).find((a) =>
      /^(竞买公告|变卖公告|拍卖公告)$/.test((a.innerText || '').trim())
    );
    if (noticeTab && noticeTab.click) noticeTab.click();
  }});
  try {{
    await page.waitForFunction(() => {{
      const href = location.href || '';
      const bodyText = (document.body && document.body.innerText) ? document.body.innerText : '';
      if (/login\\.taobao\\.com|passport\\.taobao\\.com|sec\\.taobao\\.com|punish|baxia|x5sec|verify/i.test(href)) return true;
      if (/拖动下方滑块|请按住滑块|滑动验证|滑块验证|baxia|punish|x5sec/i.test(bodyText)) return true;
      const el = document.querySelector('#J_NoticeDetail, .notice-detail, [class*="notice-content"]');
      if (!el) return false;
      const txt = (el.innerText || '').trim();
      return txt.length > 80 && !txt.includes('加载中');
    }}, undefined, {{ timeout: Math.min(CFG.timeoutMs, 6000) }});
  }} catch (_) {{}}
  await page.evaluate(() => {{
    const descEl = document.querySelector('#J_desc, #J_ItemDesc, [class*="item-desc"], [class*="survey"]');
    if (descEl && descEl.scrollIntoView) {{
      descEl.scrollIntoView({{ block: 'center' }});
    }}
  }});
  await page.waitForTimeout(500);

  const result = await page.evaluate(() => {{
    const href = location.href || '';
    const title = document.title || '';
    const bodyText = (document.body && document.body.innerText) ? document.body.innerText : '';

    const captchaRe = /拖动下方滑块|请按住滑块|滑动验证|滑块验证|完成验证|通过验证以确保正常访问|baxia|punish|x5sec/i;
    const isSliderOrRisk =
      /login\\.taobao\\.com|passport\\.taobao\\.com|sec\\.taobao\\.com|punish|baxia|x5sec|verify/i.test(href) ||
      captchaRe.test(bodyText) ||
      captchaRe.test(title) ||
      Boolean(
        document.querySelector(
          '#baxia-dialog-content, .baxia-dialog, #nc_1_n1z, .nc_iconfont, iframe[src*="punish"], iframe[src*="baxia"], iframe[src*="x5sec"]'
        )
      );
    const isLoginPage =
      /请登录|扫码登录|安全验证|人机验证/.test(bodyText) && bodyText.length < 1500;

    if (isSliderOrRisk || isLoginPage) {{
      return {{
        authRequired: true,
        reason: '详情页触发登录或滑块风控验证，请在 ego(lite) 中切入对应 Space 手动处理后重试',
        currentUrl: href,
        pageTitle: title,
      }};
    }}

    const pick = (re) => {{
      const m = bodyText.match(re);
      return m ? m[1].trim() : null;
    }};

    const h1 = document.querySelector('h1, .pm-main-l h1, [class*="title"] h1');
    const itemTitle = h1 ? (h1.innerText || '').trim() : title.replace(/-阿里拍卖.*$/, '').trim();

    const mainArea = document.querySelector('.pm-main, .pm-main-l, #J_DetailMeta, [class*="detail-head"], [class*="main-info"]');
    const headRaw = mainArea ? (mainArea.innerText || '') : '';
    const headText = (headRaw.length >= 40 ? headRaw : bodyText).slice(0, 2500);
    const pickHead = (re) => {{
      const m = headText.match(re);
      return m ? m[1].trim() : null;
    }};

    const noticeEl = document.querySelector('#J_NoticeDetail, .notice-detail, [class*="notice-content"]');
    const surveyEl = document.querySelector('#J_desc, #J_ItemDesc, [class*="item-desc"], [class*="survey"]');

    const noticeRaw = noticeEl ? (noticeEl.innerText || '').trim() : '';
    const noticeText = (noticeRaw.length >= 80 && !noticeRaw.includes('加载中')) ? noticeRaw : bodyText.slice(0, 6000);
    const surveyRaw = surveyEl ? (surveyEl.innerText || '').trim() : '';
    const surveyText = (surveyRaw.length >= 40 && !surveyRaw.includes('加载中')) ? surveyRaw : bodyText.slice(0, 6000);

    const courtEl = document.querySelector('.unit-txt, .court-name, [class*="court"], [class*="org-name"]');
    const rawCourt = courtEl ? (courtEl.innerText || '').trim() : (pick(/处置单位[：:]\\s*([^\\n]{{2,30}})/) || '');
    const courtName = (rawCourt && rawCourt.length <= 40 && !/[。，；]/.test(rawCourt)) ? rawCourt : '';

    const address = pick(/(?:标的物所在地|房产坐落|坐落|地址)[：:\\s]*([^\\n]{{4,80}})/) || '';

    return {{
      authRequired: false,
      currentUrl: href,
      pageTitle: title,
      title: itemTitle,
      stage: pickHead(/【(一拍|二拍|变卖|重新拍卖)】/) || pickHead(/(第一次拍卖|第二次拍卖|第[12]次拍卖|变卖|重新拍卖|一拍|二拍)/) || '',
      status: pickHead(/(拍卖已成交|已成交|本场已流拍|已流拍|正在进行|即将开始|已撤回|已撤拍|撤拍|已中止|已结束)/) || '',
      currentPrice: pickHead(/(?:成交价|当前价|变卖价)[：:\\s]*[¥￥]?([0-9.,]+\\s*[万亿]?)/),
      initialPrice: pickHead(/起\\s*拍\\s*价[：:\\s]*[¥￥]?([0-9.,]+\\s*[万亿]?)/),
      consultPrice: pickHead(/(?:评\\s*估\\s*价|市\\s*场\\s*价)[：:\\s]*[¥￥]?([0-9.,]+\\s*[万亿]?)/),
      depositPrice: pickHead(/保\\s*证\\s*金[：:\\s]*[¥￥]?([0-9.,]+\\s*[万亿]?)/),
      bidCount: pickHead(/(\\d+)\\s*次出价/) || pickHead(/出价记录\\s*[（(](\\d+)[）)]/),
      applyCount: pickHead(/(\\d+)\\s*人报名/),
      watchCount: pickHead(/(\\d+)\\s*次围观/),
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
        return build_list_url(
            region,
            st_param=st_param,
            page=page,
            lookback_days=getattr(self.config, "lookback_days", 0),
        )

    def _run_ego_script(self, script: str) -> dict[str, Any]:
        return self._ego.run_script(script)

    def _assert_not_captcha(self, data: dict[str, Any], *, fields: tuple[str, ...], fallback_url: str, scene: str) -> None:
        for key in fields:
            val = data.get(key)
            if val and _CAPTCHA_RISK_RE.search(str(val)):
                raise AuthRequiredError(
                    f"{scene}触发滑块或风控验证，请在 ego(lite) 中切入对应 Space 手动处理后重试",
                    url=str(data.get("currentUrl") or fallback_url),
                    task_space=self.config.task_space,
                )

    def fetch_list_page(
        self,
        region: RegionConfig,
        status_scope: str,
        page: int,
    ) -> PageResult:
        # status_scope here may already be a raw path status code ('0','1','2','-1') or logical name
        if status_scope in {"-1", "0", "1", "2", "3", "4"}:
            st_param = status_scope
        else:
            st_param = _STATUS_SCOPE_FALLBACK.get((status_scope or "").strip().lower(), "0")
        url = build_list_url(
            region,
            st_param=st_param,
            page=page,
            lookback_days=getattr(self.config, "lookback_days", 0),
        )
        script = _build_list_script(
            task_space=self.config.task_space,
            url=url,
            page_timeout=self.config.page_timeout_seconds,
        )
        data = self._run_ego_script(script)
        self._assert_not_captcha(
            data,
            fields=("currentUrl", "pageTitle", "bodyText"),
            fallback_url=url,
            scene="列表页",
        )
        if data.get("extractor") == "sf-item-list-data-error":
            raise ProviderError("淘宝列表页 #sf-item-list-data JSON 解析失败")

        items: list[RawAuctionItem] = []
        for raw in data.get("items") or []:
            item_url = _normalize_item_url(str(raw.get("url") or ""))
            item_id = str(raw.get("id") or _extract_item_id_from_url(item_url)).strip()
            title = _clean_text(raw.get("title"))
            if not item_id or not title:
                continue

            bid_count = _to_int(raw.get("bidCount"))
            raw_status = str(raw.get("status") or "").strip()
            if not raw_status:
                if st_param == "0":
                    raw_status = "ongoing"
                elif st_param == "1":
                    raw_status = "upcoming"
                elif st_param == "2":
                    raw_status = "ended"

            raw_payload = raw.get("raw") if isinstance(raw.get("raw"), dict) else raw
            stage_hint = _clean_text(raw.get("stage"))
            if not stage_hint and isinstance(raw_payload, dict) and raw_payload.get("sellOff") is True:
                stage_hint = "变卖"

            items.append(
                RawAuctionItem(
                    platform=self.platform,
                    item_id=item_id,
                    url=item_url or f"https://sf-item.taobao.com/sf_item/{item_id}.htm",
                    title=title,
                    province=region.province,
                    city=region.city,
                    stage=normalize_stage(stage_hint, title),
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
                    court_name=_clean_text(raw.get("courtName")),
                    address=_clean_text(raw.get("address")),
                    category_hint=region.category,
                    raw_payload=raw_payload,
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
        self._assert_not_captcha(
            data,
            fields=("currentUrl", "pageTitle", "title", "noticeText", "surveyText"),
            fallback_url=target_url,
            scene="详情页",
        )
        bid_count = _to_int(data.get("bidCount"))
        detail_title = _clean_text(data.get("title"))
        return RawAuctionDetail(
            platform=self.platform,
            item_id=item_id,
            url=str(data.get("currentUrl") or target_url),
            title=detail_title,
            stage=normalize_stage(_clean_text(data.get("stage")), detail_title),
            status=normalize_status(str(data.get("status") or ""), bid_count=bid_count),
            current_price=parse_price_yuan(data.get("currentPrice")),
            initial_price=parse_price_yuan(data.get("initialPrice")),
            consult_price=parse_price_yuan(data.get("consultPrice")),
            deposit_price=parse_price_yuan(data.get("depositPrice")),
            bid_count=bid_count,
            apply_count=_to_int(data.get("applyCount")),
            watch_count=_to_int(data.get("watchCount")),
            court_name=_clean_text(data.get("courtName")),
            address=_clean_text(data.get("address")),
            notice_text=str(data.get("noticeText") or "").strip(),
            survey_text=str(data.get("surveyText") or "").strip(),
            raw_payload=data,
        )
