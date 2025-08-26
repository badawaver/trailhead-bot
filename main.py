# inventory_monitor.py
import os
import re
import json
import time
import datetime as dt
import requests
from requests.cookies import create_cookie
from bs4 import BeautifulSoup, element

# ===================== 环境变量 =====================
WEBHOOK_URL     = os.getenv("DISCORD_WEBHOOK_URL")                  # Discord Webhook（可选）
INTERVAL_SEC    = int(os.getenv("INTERVAL_SEC", "600"))             # 轮询间隔
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))           # HTTP 超时
DEBUG           = os.getenv("DEBUG", "0") == "1"

# Sports Experts 相关
SPORTSEXPERTS_COOKIE = os.getenv("SPORTSEXPERTS_COOKIE", "").strip()  # 浏览器整串 Cookie（推荐保留）
AGGRESSIVE_ATC_FALLBACK = os.getenv("AGGRESSIVE_ATC_FALLBACK", "0") == "1"  # 激进兜底：源码含 add-to-cart 即视为有货
TREAT_STORE_ONLY_AS_IN_STOCK = os.getenv("TREAT_STORE_ONLY_AS_IN_STOCK", "0") == "1"  # 将 “仅门店/查看门店库存” 视为有货

# 可选代理兜底（保留，不填不影响）
SCRAPERAPI_KEY  = os.getenv("SCRAPERAPI_KEY", "").strip()

# Playwright（可选，不装就自动跳过）
PLAYWRIGHT_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "1") == "1"
PLAYWRIGHT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", str(REQUEST_TIMEOUT * 1000)))
PLAYWRIGHT_EXTRA_WAIT_MS = int(os.getenv("PLAYWRIGHT_EXTRA_WAIT_MS", "1200"))

# ===================== 常量与请求头 =====================
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/123.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en,zh-CN;q=0.9,zh;q=0.8,fr;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# 常见“无货/仅门店”文案
_AVAIL_NEG_PATTERNS = [
    "sold out", "out of stock", "currently unavailable",
    "rupture de stock", "épuisé", "indisponible"
]
_STORE_ONLY_PATTERNS = [
    "in-store only", "in store only", "see store availability"
]
_ATC_TEXT_PATTERNS = ["add to cart", "ajouter au panier", "add to bag", "add to basket"]

# ===================== 监控清单 =====================
PRODUCTS = [
    # trailhead
    {"site":"trailhead","name":"Arc'teryx Covert Cardigan Men's","url":"https://www.trailheadpaddleshack.ca/arcteryx-covert-cardigan-mens.html?id=113476423&quantity=1","color":"Cloud Heather / Void","sizes":["S","M","L"]},
    {"site":"trailhead","name":"Arc'teryx Gamma MX Hoody Men's","url":"https://www.trailheadpaddleshack.ca/arcteryx-gamma-mx-hoody-mens.html","color":"Black","sizes":["M","L"]},
    {"site":"trailhead","name":"Arc'teryx Rho LT Zip Neck Top Men's","url":"https://www.trailheadpaddleshack.ca/arcteryx-rho-lt-zip-neck-top-mens.html","color":"Black","sizes":["S","M","L","XL","XXL"]},
    {"site":"trailhead","name":"Arc'teryx Heliad 15 Backpack","url":"https://www.trailheadpaddleshack.ca/arcteryx-heliad-15-backpack.html","color":"Black","sizes":[]},
    {"site":"trailhead","name":"Arc'teryx Heliad 15 Backpack","url":"https://www.trailheadpaddleshack.ca/arcteryx-heliad-15-backpack.html","color":"Stone Green","sizes":[]},
    # sportsexperts
    {"site":"sportsexperts","name":"Arc'teryx Heliad 15 Backpack","url":"https://www.sportsexperts.ca/en-CA/p-heliad-15-compressible-backpack/435066/435066-1","color":"Black","sizes":[]},
    {"site":"sportsexperts","name":"Arc'teryx Heliad Shoulder Bag","url":"https://www.sportsexperts.ca/en-CA/p-heliad-shoulder-bag/435067/435067-1","color":"Black","sizes":[]},
    {"site":"sportsexperts","name":"Arc'teryx Rho Zip Neck Men's Baselayer Long-Sleeved Shirt","url":"https://www.sportsexperts.ca/en-CA/p-rho-zipneck-mens-baselayer-long-sleeved-shirt/230173/","color":"Black","sizes":[]},
    {"site":"sportsexperts","name":"Arc'teryx Rho Zip Neck - Women's Baselayer Long-Sleeved Shirt","url":"https://www.sportsexperts.ca/en-CA/p-rho-zipneck-womens-baselayer-long-sleeved-shirt/668324/","color":"Black","sizes":[]},
]

# ===================== 会话与工具函数 =====================
_SESSION = requests.Session()
_SESSION.headers.update(HEADERS)
_sportsexperts_inited = False

def _inject_cookie_for_domain(session: requests.Session, domain: str, cookie_str: str):
    """把浏览器整串 Cookie 精准注入到指定域"""
    if not cookie_str.strip():
        return
    for part in cookie_str.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip(); v = v.strip()
            if not k:
                continue
            ck = create_cookie(name=k, value=v, domain=domain, path="/")
            session.cookies.set_cookie(ck)

def _is_incapsula_block(html: str) -> bool:
    """更激进的防护页识别"""
    low = (html or "").lower()
    tokens = [
        "_incapsula_resource", "incapsula", "request unsuccessful",
        "/_/incapsula_resource?", "visid_incap", "incap_ses"
    ]
    return any(t in low for t in tokens)

def _warmup_sportsexperts():
    """预热并注入 Cookie，提升真实页面命中率"""
    global _sportsexperts_inited
    if _sportsexperts_inited:
        return
    if SPORTSEXPERTS_COOKIE:
        _inject_cookie_for_domain(_SESSION, ".sportsexperts.ca", SPORTSEXPERTS_COOKIE)
        _inject_cookie_for_domain(_SESSION, "www.sportsexperts.ca", SPORTSEXPERTS_COOKIE)
        _SESSION.headers["Cookie"] = SPORTSEXPERTS_COOKIE
        if DEBUG:
            names = [c.name for c in _SESSION.cookies if "sportsexperts.ca" in (c.domain or "")]
            print("[DEBUG] 注入 Cookie 名称：", names, flush=True)
            print("[DEBUG] Cookie 头长度：", len(SPORTSEXPERTS_COOKIE), flush=True)
    _SESSION.headers.update({
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    })
    try:
        _SESSION.get("https://www.sportsexperts.ca/en-CA/", timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except Exception as e:
        if DEBUG: print(f"[DEBUG] 预热失败: {e}", flush=True)
    _sportsexperts_inited = True

def _debug_save_html(tag: str, url: str, html: str):
    if not DEBUG:
        return
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", tag)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = f"/tmp/{safe}_{ts}.html"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"<!-- {url} -->\n")
            f.write(html)
        print(f"[DEBUG] 保存 HTML：{path}", flush=True)
    except Exception as e:
        print(f"[DEBUG] 保存 HTML 失败：{e}", flush=True)

# ---- Playwright 兜底（可选）----
def _http_get_via_playwright(url: str) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        if DEBUG: print(f"[DEBUG] Playwright 未安装/导入失败: {e}", flush=True)
        return ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=PLAYWRIGHT_HEADLESS)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"], locale="en-CA", timezone_id="America/Toronto",
            )
            if SPORTSEXPERTS_COOKIE:
                cookies = []
                for part in SPORTSEXPERTS_COOKIE.split(";"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        k = k.strip(); v = v.strip()
                        if not k: continue
                        cookies.append({"name": k, "value": v, "domain": "www.sportsexperts.ca", "path": "/"})
                        cookies.append({"name": k, "value": v, "domain": ".sportsexperts.ca", "path": "/"})
                if cookies:
                    context.add_cookies(cookies)
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT_MS)
            for text in ["Accept", "I agree", "Got it", "OK"]:
                try:
                    page.get_by_role("button", name=text).first.click(timeout=1500)
                    break
                except Exception:
                    pass
            page.wait_for_timeout(PLAYWRIGHT_EXTRA_WAIT_MS)
            html = page.content()
            context.close(); browser.close()
            return html
    except Exception as e:
        if DEBUG: print(f"[DEBUG] Playwright 渲染失败: {e}", flush=True)
        return ""

# ---- ScraperAPI 兜底（可选）----
def _http_get_via_scraperapi(url: str) -> str:
    if not SCRAPERAPI_KEY:
        return ""
    try:
        api = "http://api.scraperapi.com"
        params = {"api_key": SCRAPERAPI_KEY, "render": "true", "country_code": "ca", "url": url}
        r = requests.get(api, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception as e:
        if DEBUG: print(f"[DEBUG] ScraperAPI 失败: {e}", flush=True)
        return ""

def http_get(url: str) -> str:
    if "sportsexperts.ca" in url:
        _warmup_sportsexperts()
    delay = 0.4
    last_err = None
    for _ in range(5):
        try:
            r = _SESSION.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            r.raise_for_status()
            text = r.text
            if DEBUG and "sportsexperts.ca" in url:
                _debug_save_html("sportsexperts_page", url, text)
                print("[sportsexperts][DEBUG] 页面长度:", len(text), flush=True)
            # 不在这里短路，交由解析函数决定 Unknown/True/False
            return text
        except Exception as e:
            last_err = e
            time.sleep(min(5.0, delay))
            delay *= 1.8
    if last_err:
        raise last_err

# ===================== Discord 推送 =====================
def send_discord_message(text: str):
    if not WEBHOOK_URL:
        print("WARN: 未设置 DISCORD_WEBHOOK_URL，跳过发送", flush=True)
        return
    while text:
        chunk = text[:1900]
        text = text[1900:]
        try:
            r = requests.post(WEBHOOK_URL, json={"content": chunk}, timeout=15)
            if r.status_code not in (200, 204):
                print("Discord 发送失败:", r.status_code, r.text, flush=True)
        except Exception as e:
            print("Discord 请求异常:", e, flush=True)

# ===================== Trailhead 解析 =====================
def check_stock_trailhead(url: str, color: str, sizes: list):
    html = http_get(url)
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", id="prodattr2")
    if not select:
        if DEBUG: print("[trailhead] 未找到 #prodattr2，视为不可选", flush=True)
        return {"__any__": False} if not sizes else {s: False for s in sizes}
    options = select.find_all("option")
    if not sizes:  # 仅看颜色
        available = any((opt.get("data-color", "").strip() == color) and (not opt.has_attr("disabled"))
                        for opt in options)
        if DEBUG: print(f"[trailhead] {color} -> {'有货' if available else '无货'} (无尺码)", flush=True)
        return {"__any__": available}
    # 有尺码
    stock_status = {size: False for size in sizes}
    for opt in options:
        if opt.get("data-color", "").strip() == color:
            sz = opt.get("data-size", "").strip()
            if sz in stock_status:
                stock_status[sz] = not opt.has_attr("disabled")
    if DEBUG: print(f"[trailhead] {color} 尺码状态: {stock_status}", flush=True)
    return stock_status

# ===================== Sports Experts 解析 =====================
def _text_has_any(hay: str, needles: list) -> bool:
    hay = (hay or "").lower()
    return any(n in hay for n in needles)

def _is_element_enabled(el: element.Tag) -> bool:
    if "disabled" in el.attrs: return False
    if (el.get("aria-disabled") or "").lower() in ("true", "1"): return False
    if (el.get("aria-hidden") or "").lower() in ("true", "1"): return False
    if el.has_attr("hidden"): return False
    if (el.get("data-available") or "").lower() in ("false", "0"): return False
    self_tokens = {c.lower() for c in (el.get("class") or []) if isinstance(c, str)}
    if self_tokens & {"disabled","is-disabled","disabled-button","soldout","is-hidden","sr-only"}:
        return False
    style = (el.get("style") or "").replace(" ", "").lower()
    if any(s in style for s in ["display:none","visibility:hidden","pointer-events:none","opacity:0"]): return False
    parent = el.parent; depth = 0
    while parent is not None and depth < 4:
        if isinstance(parent, element.Tag):
            if (parent.get("aria-hidden") or "").lower() in ("true","1"): return False
            if parent.has_attr("hidden"): return False
            pstyle = (parent.get("style") or "").replace(" ", "").lower()
            if any(s in pstyle for s in ["display:none","visibility:hidden"]): return False
            ptokens = {c.lower() for c in (parent.get("class") or []) if isinstance(c, str)}
            if ptokens & {"d-none","hidden","visually-hidden","sr-only","is-hidden"}: return False
        parent = parent.parent; depth += 1
    return True

def _has_add_to_cart(soup_scope: BeautifulSoup) -> bool:
    candidates = soup_scope.select(
        "button, a[role='button'], input[type='submit'], "
        "[data-qa*='add-to-cart' i], [data-qa='product-add-to-cart' i], "
        "[data-oc-click*='addlineitem' i]"
    )
    for el in candidates:
        label = " ".join(filter(None, [
            (el.get_text(" ", strip=True) or "").lower(),
            (el.get("value") or "").lower(),
            (el.get("aria-label") or "").lower(),
            (el.get("title") or "").lower(),
            (el.get("data-qa") or "").lower(),
            (el.get("data-oc-click") or "").lower(),
        ]))
        if any(p in label for p in _ATC_TEXT_PATTERNS) or ("product-add-to-cart" in label) or ("addlineitem" in label):
            if _is_element_enabled(el):
                return True
    return False

def _scan_avail_keys(node):
    yes_tokens = {"instock","in stock","available","sellable","true"}
    no_tokens  = {"outofstock","out of stock","unavailable","false"}
    if isinstance(node, dict):
        for k, v in node.items():
            kl = str(k).lower()
            if any(t in kl for t in ["availability","avail","stock","inventory","in_stock","instock"]):
                sv = str(v).lower()
                if any(t in sv for t in yes_tokens): return True
                if any(t in sv for t in no_tokens):  return False
            res = _scan_avail_keys(v)
            if res is not None: return res
    elif isinstance(node, list):
        for it in node:
            res = _scan_avail_keys(it)
            if res is not None: return res
    return None

def _parse_jsonld_availability(soup: BeautifulSoup):
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.text or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        vals = []
        def scan(node):
            if isinstance(node, dict):
                if "availability" in node: vals.append(str(node["availability"]).lower())
                if "offers" in node: scan(node["offers"])
                for v in node.values(): scan(v)
            elif isinstance(node, list):
                for it in node: scan(it)
        scan(data)
        if vals:
            if any("instock" in v for v in vals): return True
            if any("outofstock" in v for v in vals): return False
    return None

def _parse_microdata_availability(soup: BeautifulSoup):
    for tag in soup.select('[itemprop="availability"]'):
        val = (tag.get("href") or tag.get("content") or tag.get_text()).lower()
        if "instock" in val: return True
        if "outofstock" in val: return False
    return None

def _parse_inline_json_availability(html: str):
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S|re.I)
    for raw in scripts:
        if not any(k in raw for k in ('"availability"', '"inStock"', '"available"', '"inventory"')):
            continue
        for m in re.finditer(r"(\{.*?\}|\[.*?\])", raw, flags=re.S):
            snippet = m.group(1)
            if snippet.count("{") != snippet.count("}"):
                continue
            try:
                data = json.loads(snippet)
            except Exception:
                continue
            found = _scan_avail_keys(data)
            if found is not None:
                return found
    return None

def _any_size_enabled(soup_scope: BeautifulSoup) -> bool:
    size_words = {"xs","s","m","l","xl","xxl"}
    elems = soup_scope.select("button, a[role='button'], [data-size], [data-variant], [data-qa*='size' i]")
    for el in elems:
        if not isinstance(el, element.Tag): continue
        txt = (el.get_text(strip=True) or el.get("data-size") or el.get("aria-label") or "").lower()
        if txt in size_words and _is_element_enabled(el):
            return True
    return False

def check_stock_sportsexperts(url: str):
    """
    返回：True(有货) / False(无货) / None(未知/被防护)
    判定顺序：
      0) 命中防护页 -> Unknown
      1) JSON-LD / microdata / 内联 JSON 的 availability
      2) 商品作用域内可点击的 Add to Cart
      3) 商品作用域内可选尺码
      4) 激进兜底（源码含 add-to-cart 且无负面文案）
      5) “仅门店/查看门店库存” -> 受 TREAT_STORE_ONLY_AS_IN_STOCK 控制
      6) 常见无货文案
    """
    html = http_get(url)
    if DEBUG:
        print("[sportsexperts][DEBUG] 页面长度:", len(html), flush=True)

    # 0) 命中防护页，尝试一次或两次兜底，否则 Unknown
    if _is_incapsula_block(html):
        if DEBUG: print("[sportsexperts][DEBUG] 疑似防护页", flush=True)
        alt = _http_get_via_playwright(url)
        if alt and not _is_incapsula_block(alt):
            html = alt
        else:
            alt2 = _http_get_via_scraperapi(url)
            if alt2 and not _is_incapsula_block(alt2):
                html = alt2
            else:
                if DEBUG: print("[sportsexperts][DEBUG] 防护仍在，返回 Unknown", flush=True)
                return None

    soup = BeautifulSoup(html, "html.parser")

    # 1) 结构化/内联 JSON
    avail = _parse_jsonld_availability(soup)
    if avail is None: avail = _parse_microdata_availability(soup)
    if avail is None: avail = _parse_inline_json_availability(html)
    if avail is True:
        if DEBUG: print("[sportsexperts] availability(JSON) => True", flush=True)
        return True
    if avail is False:
        if DEBUG: print("[sportsexperts] availability(JSON) => False", flush=True)
        return False

    # 定义商品作用域（降低误检）
    product_root = soup.select_one("form, [data-qa='product-page'], .pdp, .product-page, .product-detail")
    scope = product_root if product_root else soup

    # 2) Add to Cart（限定在商品表单容器内）
    if _has_add_to_cart(scope):
        if DEBUG: print("[sportsexperts] 可点击 Add to Cart => True", flush=True)
        return True

    # 3) 尺码按钮（限定作用域）
    if _any_size_enabled(scope):
        if DEBUG: print("[sportsexperts] 可选尺码 => True", flush=True)
        return True

    # 4) 激进兜底（源码关键字 & 无负面文案）
    raw = html.lower()
    if AGGRESSIVE_ATC_FALLBACK and (("product-add-to-cart" in raw) or ("addlineitem" in raw) or ("add to cart" in raw)):
        plain = soup.get_text(" ", strip=True).lower()
        if not _text_has_any(plain, _AVAIL_NEG_PATTERNS + _STORE_ONLY_PATTERNS):
            if DEBUG: print("[sportsexperts] 兜底：源码含 add-to-cart => True", flush=True)
            return True

    # 5) 仅门店
    plain = soup.get_text(" ", strip=True).lower()
    if _text_has_any(plain, _STORE_ONLY_PATTERNS):
        if DEBUG: print("[sportsexperts] 命中仅门店/查询门店库存", flush=True)
        return True if TREAT_STORE_ONLY_AS_IN_STOCK else False

    # 6) 无货文案
    if _text_has_any(plain, _AVAIL_NEG_PATTERNS):
        if DEBUG: print("[sportsexperts] 文案无货 => False", flush=True)
        return False

    if DEBUG: print("[sportsexperts] 未识别明确有货信号 => False", flush=True)
    return False

# ===================== 主循环 =====================
def main():
    print("开始监控多个商品库存状态...", flush=True)
    last_status_all = {}

    while True:
        all_messages = []

        for p in PRODUCTS:
            site, name, url, color, sizes = p["site"], p["name"], p["url"], p["color"], p["sizes"]
            key = (site, name, color)
            try:
                if site == "trailhead":
                    current = check_stock_trailhead(url, color, sizes)
                    last = last_status_all.get(key, {})
                    if sizes:
                        if current != last:
                            ins = [s for s, ok in current.items() if ok]
                            outs = [s for s, ok in current.items() if not ok]
                            msg = f"trailhead {name} - {color}\n"
                            if ins:  msg += "✅ 有库存: " + ", ".join(ins) + "\n"
                            if outs: msg += "❌ 无库存: " + ", ".join(outs)
                            all_messages.append(msg)
                            last_status_all[key] = current
                        print(f"[trailhead] {name} - {color} 状态: {current}", flush=True)
                    else:
                        available = bool(current.get("__any__", False))
                        last_available = (last.get("__any__", None) if isinstance(last, dict) else None)
                        if available != last_available:
                            msg = f"trailhead {name} - {color}\n" + ("✅ 有库存" if available else "❌ 无库存")
                            all_messages.append(msg)
                            last_status_all[key] = current
                        print(f"[trailhead] {name} - {color} 状态: {'有货' if available else '无货'}", flush=True)

                elif site == "sportsexperts":
                    in_stock = check_stock_sportsexperts(url)
                    if in_stock is None:
                        # Unknown：不更新状态，不推送
                        print(f"[sportsexperts] {name} - {color} 状态: 未知(被防护)", flush=True)
                        continue
                    last = last_status_all.get(key)
                    if in_stock != last:
                        msg = f"sportsexperts {name} - {color}\n" + ("✅ 有库存" if in_stock else "❌ 无库存")
                        all_messages.append(msg)
                        last_status_all[key] = in_stock
                    print(f"[sportsexperts] {name} - {color} 状态: {'有货' if in_stock else '无货'}", flush=True)

                else:
                    print(f"未知站点: {site}，已跳过", flush=True)

            except requests.HTTPError as e:
                print(f"请求失败 {site} {name} - {color}: HTTP {e.response.status_code}", flush=True)
            except Exception as e:
                print(f"请求失败 {site} {name} - {color}: {e}", flush=True)

        if all_messages:
            send_discord_message("\n\n".join(all_messages))

        time.sleep(INTERVAL_SEC)

if __name__ == "__main__":
    main()
