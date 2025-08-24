import os
import re
import json
import time
import datetime as dt
import requests
from requests.cookies import create_cookie   # <<< 新增：用于按域名注入 Cookie
from bs4 import BeautifulSoup, element

# ===== 环境变量 =====
WEBHOOK_URL     = os.getenv("DISCORD_WEBHOOK_URL")          # Railway 配置
INTERVAL_SEC    = int(os.getenv("INTERVAL_SEC", "600"))     # 轮询间隔（秒）
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))   # 请求超时（秒）
DEBUG           = os.getenv("DEBUG", "0") == "1"
SPORTSEXPERTS_COOKIE = os.getenv("SPORTSEXPERTS_COOKIE", "").strip()
AGGRESSIVE_ATC_FALLBACK = os.getenv("AGGRESSIVE_ATC_FALLBACK", "0") == "1"
SCRAPERAPI_KEY  = os.getenv("SCRAPERAPI_KEY", "").strip()   # <<< 可选：代理渲染兜底

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/123.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en,zh-CN;q=0.9,zh;q=0.8,fr;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# ===== 商品清单 =====
PRODUCTS = [
    # ----- trailhead -----
    {"site":"trailhead","name":"Arc'teryx Covert Cardigan Men's","url":"https://www.trailheadpaddleshack.ca/arcteryx-covert-cardigan-mens.html?id=113476423&quantity=1","color":"Cloud Heather / Void","sizes":["S","M","L"]},
    {"site":"trailhead","name":"Arc'teryx Gamma MX Hoody Men's","url":"https://www.trailheadpaddleshack.ca/arcteryx-gamma-mx-hoody-mens.html","color":"Black","sizes":["M","L"]},
    {"site":"trailhead","name":"Arc'teryx Rho LT Zip Neck Top Men's","url":"https://www.trailheadpaddleshack.ca/arcteryx-rho-lt-zip-neck-top-mens.html","color":"Black","sizes":["S","M","L","XL","XXL"]},
    {"site":"trailhead","name":"Arc'teryx Heliad 15 Backpack","url":"https://www.trailheadpaddleshack.ca/arcteryx-heliad-15-backpack.html","color":"Black","sizes":[]},
    {"site":"trailhead","name":"Arc'teryx Heliad 15 Backpack","url":"https://www.trailheadpaddleshack.ca/arcteryx-heliad-15-backpack.html","color":"Stone Green","sizes":[]},
    # ----- sports experts -----
    {"site":"sportsexperts","name":"Arc'teryx Heliad 15 Backpack","url":"https://www.sportsexperts.ca/en-CA/p-heliad-15-compressible-backpack/435066/435066-1","color":"Black","sizes":[]},
    {"site":"sportsexperts","name":"Arc'teryx Heliad Shoulder Bag","url":"https://www.sportsexperts.ca/en-CA/p-heliad-shoulder-bag/435067/435067-1","color":"Black","sizes":[]},
    {"site":"sportsexperts","name":"Arc'teryx Rho Zip Neck Men's Baselayer Long-Sleeved Shirt","url":"https://www.sportsexperts.ca/en-CA/p-rho-zipneck-mens-baselayer-long-sleeved-shirt/230173/","color":"Black","sizes":[]},
    {"site":"sportsexperts","name":"Arc'teryx Rho Zip Neck - Women's Baselayer Long-Sleeved Shirt","url":"https://www.sportsexperts.ca/en-CA/p-rho-zipneck-womens-baselayer-long-sleeved-shirt/668324/","color":"Black","sizes":[]},
]

# ===== HTTP: Session + 预热 + Incapsula 检测 =====
_SESSION = requests.Session()
_SESSION.headers.update(HEADERS)

def _parse_cookie_string(cookie_str: str) -> dict:
    jar = {}
    for part in cookie_str.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip()
    return jar

def _inject_cookie_for_domain(session: requests.Session, domain: str, cookie_str: str):
    """把浏览器拷贝的整串 Cookie 按域名精准注入到 session"""
    if not cookie_str.strip():
        return
    for part in cookie_str.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            v = v.strip()
            if not k:
                continue
            ck = create_cookie(name=k, value=v, domain=domain, path="/")
            session.cookies.set_cookie(ck)

def _is_incapsula_block(html: str) -> bool:
    low = (html or "").lower()
    return ("_incapsula_resource" in low) or ('name="robots"' in low and "noindex" in low)

_sportsexperts_inited = False
def _warmup_sportsexperts():
    """预热域并注入浏览器 Cookie，尽量拿到真实页面"""
    global _sportsexperts_inited
    if _sportsexperts_inited:
        return

    if SPORTSEXPERTS_COOKIE:
        # 1) 精准注入到 cookiejar
        _inject_cookie_for_domain(_SESSION, ".sportsexperts.ca", SPORTSEXPERTS_COOKIE)
        _inject_cookie_for_domain(_SESSION, "www.sportsexperts.ca", SPORTSEXPERTS_COOKIE)
        # 2) 同时强制作为请求头发送，确保会被带出去
        _SESSION.headers["Cookie"] = SPORTSEXPERTS_COOKIE

        if DEBUG:
            names = [c.name for c in _SESSION.cookies if "sportsexperts.ca" in (c.domain or "")]
            print("[DEBUG] 已注入 sportsexperts Cookie 名称：", names, flush=True)
            print("[DEBUG] Cookie 头长度：", len(SPORTSEXPERTS_COOKIE), flush=True)

    # 更像浏览器的一些头（可选）
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
        if DEBUG: print(f"[DEBUG] SportsExperts 预热失败: {e}", flush=True)

    _sportsexperts_inited = True

def _debug_save_html(site: str, name: str, url: str, html: str):
    if not DEBUG:
        return
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", f"{site}_{name}")
    ts   = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = f"/tmp/{safe}_{ts}.html"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"<!-- {url} -->\n")
            f.write(html)
        print(f"[DEBUG] 已保存 HTML 快照: {path}", flush=True)
    except Exception as e:
        print(f"[DEBUG] 保存 HTML 快照失败: {e}", flush=True)

# 可选：命中拦截时用 ScraperAPI 渲染兜底（未配置 key 时不会触发）
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
        if DEBUG: print(f"[DEBUG] ScraperAPI 调用失败: {e}", flush=True)
        return ""

def http_get(url: str) -> str:
    if "sportsexperts.ca" in url:
        _warmup_sportsexperts()
    delay = 0.4
    last_err = None
    for attempt in range(5):
        try:
            r = _SESSION.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            r.raise_for_status()
            text = r.text

            if DEBUG and "sportsexperts.ca" in url:
                _debug_save_html("sportsexperts", "page", url, text)
                print("[sportsexperts][DEBUG] 页面长度:", len(text), flush=True)

            # 命中 Incapsula 拦截：尝试代理渲染兜底
            if "sportsexperts.ca" in url and _is_incapsula_block(text):
                print("[sportsexperts] 命中 Incapsula 拦截页（可注入 SPORTSEXPERTS_COOKIE，或用浏览器渲染）", flush=True)
                alt = _http_get_via_scraperapi(url)
                if alt:
                    if DEBUG: print("[sportsexperts][DEBUG] 代理渲染长度:", len(alt), flush=True)
                    return alt

            return text
        except Exception as e:
            last_err = e
            time.sleep(min(5.0, delay))
            delay *= 1.8
    if last_err:
        raise last_err

# ===== Discord 发送 =====
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

# ===== Trailhead 库存检测 =====
def check_stock_trailhead(url: str, color: str, sizes: list):
    """
    sizes 为空：只判断该颜色是否可选（__any__）
    返回：
      - 有尺码：{size: bool, ...}
      - 无尺码：{"__any__": bool}
    """
    html = http_get(url)
    soup = BeautifulSoup(html, "html.parser")

    select = soup.find("select", id="prodattr2")
    if not select:
        if DEBUG: print("[trailhead] 未找到 #prodattr2，视为不可选", flush=True)
        return {"__any__": False} if not sizes else {s: False for s in sizes}

    options = select.find_all("option")

    if not sizes:  # 无尺码，仅看颜色是否存在且未禁用
        available = any(
            (opt.get("data-color", "").strip() == color) and (not opt.has_attr("disabled"))
            for opt in options
        )
        if DEBUG: print(f"[trailhead] {color} -> {'有货' if available else '无货'} (无尺码)", flush=True)
        return {"__any__": available}

    # 有尺码
    stock_status = {size: False for size in sizes}
    for opt in options:
        if opt.get("data-color", "").strip() == color:
            opt_size = opt.get("data-size", "").strip()
            if opt_size in stock_status:
                stock_status[opt_size] = not opt.has_attr("disabled")
    if DEBUG: print(f"[trailhead] {color} 尺码状态: {stock_status}", flush=True)
    return stock_status

# ===== 辅助：Sports Experts 解析 =====
_AVAIL_NEG_PATTERNS = [
    "sold out", "out of stock", "currently unavailable",
    "in-store only", "in store only", "see store availability",  # 门店专售：线上视为无货
    "rupture de stock", "épuisé", "indisponible"  # 法语兜底
]
_BTN_TEXT_PATTERNS = [
    "add to cart", "ajouter au panier",
    "add to bag", "add to basket"
]

def _text_has_any(hay: str, needles: list) -> bool:
    hay = (hay or "").lower()
    return any(n in hay for n in needles)

def _is_element_enabled(el: element.Tag) -> bool:
    # 自身禁用/隐藏属性
    if "disabled" in el.attrs: return False
    if (el.get("aria-disabled") or "").lower() in ("true", "1"): return False
    if (el.get("aria-hidden") or "").lower() in ("true", "1"): return False
    if el.has_attr("hidden"): return False
    if (el.get("data-available") or "").lower() in ("false", "0"): return False

    # 自身 class：只拦强隐藏/禁用 token，避免把 hidden-xs 当隐藏
    self_tokens = {c.lower() for c in (el.get("class") or []) if isinstance(c, str)}
    if self_tokens & {"disabled", "is-disabled", "disabled-button", "soldout", "is-hidden", "sr-only"}:
        return False

    # 自身 style 隐藏
    style = (el.get("style") or "").replace(" ", "").lower()
    if any(s in style for s in ["display:none", "visibility:hidden", "pointer-events:none", "opacity:0"]):
        return False

    # 祖先隐藏（忽略 bootstrap 的 hidden-xs/hidden-sm）
    parent = el.parent
    depth = 0
    while parent is not None and depth < 4:
        if isinstance(parent, element.Tag):
            if (parent.get("aria-hidden") or "").lower() in ("true", "1"): return False
            if parent.has_attr("hidden"): return False
            pstyle = (parent.get("style") or "").replace(" ", "").lower()
            if any(s in pstyle for s in ["display:none", "visibility:hidden"]):
                return False
            ptokens = {c.lower() for c in (parent.get("class") or []) if isinstance(c, str)}
            if ptokens & {"d-none", "hidden", "visually-hidden", "sr-only", "is-hidden"}:
                return False
        parent = parent.parent
        depth += 1
    return True

def _has_add_to_cart(soup: BeautifulSoup) -> bool:
    # 覆盖 button / a[role=button] / input[type='submit'] / data-qa / data-oc-click
    candidates = soup.select(
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
        if any(pat in label for pat in _BTN_TEXT_PATTERNS) or \
           ("product-add-to-cart" in label) or ("addlineitem" in label):
            if _is_element_enabled(el):
                return True
    return False

def _scan_avail_keys(node):
    """递归扫描典型库存键，返回 True/False/None"""
    yes_tokens = {"instock", "in stock", "available", "sellable", "true"}
    no_tokens  = {"outofstock", "out of stock", "unavailable", "false"}
    if isinstance(node, dict):
        for k, v in node.items():
            kl = str(k).lower()
            if any(t in kl for t in ["availability", "avail", "stock", "inventory", "in_stock", "instock"]):
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
    """从 JSON-LD 里读取 availability。返回 True/False/None（None 表示未识别）"""
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
                if "availability" in node:
                    vals.append(str(node["availability"]).lower())
                if "offers" in node:
                    scan(node["offers"])
                for v in node.values():
                    scan(v)
            elif isinstance(node, list):
                for it in node:
                    scan(it)
        scan(data)
        if vals:
            if any("instock" in v for v in vals):
                return True
            if any("outofstock" in v for v in vals):
                return False
    return None

def _parse_microdata_availability(soup: BeautifulSoup):
    """从 microdata/link/meta 读取 availability。返回 True/False/None"""
    for tag in soup.select('[itemprop="availability"]'):
        val = (tag.get("href") or tag.get("content") or tag.get_text()).lower()
        if "instock" in val:
            return True
        if "outofstock" in val:
            return False
    return None

def _parse_inline_json_availability(html: str):
    """从内联 <script> 里粗粒度提取 JSON，并扫描库存键"""
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

def _any_size_enabled(soup: BeautifulSoup) -> bool:
    """有些页面须先选尺码才激活按钮：发现任一可点击尺码则视为“某尺码有货”"""
    size_words = {"xs","s","m","l","xl","xxl"}
    elems = soup.select("button, a[role='button'], [data-size], [data-variant], [data-qa*='size' i]")
    for el in elems:
        if not isinstance(el, element.Tag):
            continue
        txt = (el.get_text(strip=True) or el.get("data-size") or el.get("aria-label") or "").lower()
        if txt in size_words and _is_element_enabled(el):
            return True
    return False

# ===== Sports Experts 库存检测 =====
def check_stock_sportsexperts(url: str) -> bool:
    """
    判定次序：
      1) JSON-LD / microdata / 内联 JSON 的 availability
      2) 可见且未禁用的“Add to cart”按钮
      3) （可选）激进兜底：源码含 product-add-to-cart / addLineItem
      4) 页面出现 Sold out / Out of stock / In-Store Only 等字样 -> 线上无货
      5) 其他情况保守返回 False（无货）
    """
    html = http_get(url)
    soup = BeautifulSoup(html, "html.parser")

    if DEBUG:
        print("[sportsexperts][DEBUG] 页面长度:", len(html), flush=True)
        if _is_incapsula_block(html):
            print("[sportsexperts][DEBUG] 疑似防护页，占位 HTML。", flush=True)

    # 1) 结构化/内联 JSON（最稳）
    avail = _parse_jsonld_availability(soup)
    if avail is None:
        avail = _parse_microdata_availability(soup)
    if avail is None:
        avail = _parse_inline_json_availability(html)
    if avail is True:
        if DEBUG: print("[sportsexperts] availability(JSON) => True", flush=True)
        return True
    if avail is False:
        if DEBUG: print("[sportsexperts] availability(JSON) => False", flush=True)
        return False

    # 2) 可点击的 Add to Cart
    if _has_add_to_cart(soup):
        if DEBUG: print("[sportsexperts] 可点击 Add to Cart => True", flush=True)
        return True

    # 2.5) 可选兜底：发现可选尺码也认为“有货”
    if _any_size_enabled(soup):
        if DEBUG: print("[sportsexperts] 检到可选尺码 => 视为有货(True)", flush=True)
        return True

    # 3) 激进兜底（可配置）：源码含 add-to-cart 关键字，但选择器未命中
    raw = html.lower()
    if AGGRESSIVE_ATC_FALLBACK and (("product-add-to-cart" in raw) or ("addlineitem" in raw) or ("add to cart" in raw)):
        plain = soup.get_text(" ", strip=True).lower()
        if not any(x in plain for x in _AVAIL_NEG_PATTERNS):
            if DEBUG: print("[sportsexperts] 兜底：源码含 add-to-cart 关键字 => True", flush=True)
            return True
        else:
            if DEBUG: print("[sportsexperts] 兜底被文案否决（有无货/门店专售提示）", flush=True)

    # 4) 常见“无货/仅门店”文案（线上视为无货）
    plain = soup.get_text(" ", strip=True).lower()
    if any(x in plain for x in _AVAIL_NEG_PATTERNS):
        if DEBUG: print("[sportsexperts] 文案命中无货/门店专售 => False", flush=True)
        return False

    if DEBUG: print("[sportsexperts] 未识别到明确有货信号 => False", flush=True)
    return False

# ===== 主循环 =====
if __name__ == "__main__":
    print("开始监控多个商品库存状态...", flush=True)
    last_status_all = {}

    while True:
        all_messages = []

        for product in PRODUCTS:
            site  = product["site"]
            name  = product["name"]
            url   = product["url"]
            color = product["color"]
            sizes = product["sizes"]

            key = (site, name, color)

            try:
                if site == "trailhead":
                    current_status = check_stock_trailhead(url, color, sizes)
                    last_status    = last_status_all.get(key, {})

                    if sizes:  # 有尺码
                        if current_status != last_status:
                            in_stock  = [s for s, ok in current_status.items() if ok]
                            out_stock = [s for s, ok in current_status.items() if not ok]
                            msg = f"trailhead {name} - {color}\n"
                            if in_stock:
                                msg += "✅ 有库存: " + ", ".join(in_stock) + "\n"
                            if out_stock:
                                msg += "❌ 无库存: " + ", ".join(out_stock)
                            all_messages.append(msg)
                            last_status_all[key] = current_status
                        print(f"[trailhead] {name} - {color} 状态: {current_status}", flush=True)

                    else:      # 无尺码
                        available = bool(current_status.get("__any__", False))
                        last_available = (
                            last_status.get("__any__", None) if isinstance(last_status, dict) else None
                        )
                        if available != last_available:
                            msg = f"trailhead {name} - {color}\n"
                            msg += "✅ 有库存" if available else "❌ 无库存"
                            all_messages.append(msg)
                            last_status_all[key] = current_status
                        print(f"[trailhead] {name} - {color} 状态: {'有货' if available else '无货'}", flush=True)

                elif site == "sportsexperts":
                    in_stock = check_stock_sportsexperts(url)
                    last_status = last_status_all.get(key)  # bool 或 None
                    if in_stock != last_status:
                        msg = f"sportsexperts {name} - {color}\n"
                        msg += "✅ 有库存" if in_stock else "❌ 无库存"
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
