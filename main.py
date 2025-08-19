import os
import re
import json
import time
import datetime as dt
import requests
from bs4 import BeautifulSoup, element

# ===== 环境变量 =====
WEBHOOK_URL     = os.getenv("DISCORD_WEBHOOK_URL")          # Railway 配置
INTERVAL_SEC    = int(os.getenv("INTERVAL_SEC", "600"))     # 轮询间隔（秒）
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))   # 请求超时（秒）
DEBUG           = os.getenv("DEBUG", "0") == "1"
# 可选：从浏览器复制 sportsexperts.ca 的整串 Cookie 注入
SPORTSEXPERTS_COOKIE = os.getenv("SPORTSEXPERTS_COOKIE", "").strip()

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/123.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en,zh-CN;q=0.9,zh;q=0.8,fr;q=0.7",
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

# ===== HTTP（带 Session）=====
_SESSION = requests.Session()
_SESSION.headers.update(HEADERS)

def _parse_cookie_string(cookie_str: str) -> dict:
    jar = {}
    for part in cookie_str.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip()
    return jar

def _is_incapsula_block(html: str) -> bool:
    low = (html or "").lower()
    # 特征：NOINDEX/NOFOLLOW + /_Incapsula_Resource 或 iframe 跳转
    return ("_incapsula_resource" in low) or ('name="robots"' in low and "noindex" in low)

_sportsexperts_inited = False
def _warmup_sportsexperts():
    """预热同域、可选注入浏览器 Cookie，并加一些更像浏览器的头"""
    global _sportsexperts_inited
    if _sportsexperts_inited:
        return
    if SPORTSEXPERTS_COOKIE:
        _SESSION.cookies.update(_parse_cookie_string(SPORTSEXPERTS_COOKIE))
    _SESSION.headers.update({
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    try:
        resp = _SESSION.get("https://www.sportsexperts.ca/en-CA/", timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if DEBUG and _is_incapsula_block(resp.text):
            print("[DEBUG] SportsExperts 首页被 Incapsula 拦截（需要 Cookie 或浏览器渲染）", flush=True)
    except Exception as e:
        if DEBUG: print(f"[DEBUG] SportsExperts 预热失败: {e}", flush=True)
    _sportsexperts_inited = True

def http_get(url: str) -> str:
    # 对 sportsexperts 先预热
    if "sportsexperts.ca" in url:
        _warmup_sportsexperts()
    last_err = None
    for _ in range(3):
        try:
            r = _SESSION.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            r.raise_for_status()
            text = r.text
            if "sportsexperts.ca" in url and _is_incapsula_block(text):
                if DEBUG:
                    print("[DEBUG] 命中 Incapsula 拦截页（返回占位 HTML）", flush=True)
                    print("[DEBUG] 拦截页片段:", text[:300].replace("\n", " "), flush=True)
            return text
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    if last_err:
        raise last_err

# ===== DEBUG：保存 + 打印 HTML 片段 =====
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
        snippet = html[:1500].replace("\n", " ")
        print(f"[DEBUG] HTML 片段预览: {snippet}", flush=True)
    except Exception as e:
        print(f"[DEBUG] 保存 HTML 快照失败: {e}", flush=True)

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

# ===== Sports Experts 辅助函数 =====
_AVAIL_NEG_PATTERNS = [
    "sold out", "out of stock", "currently unavailable",
    "not available", "online only - out of stock",
    "in-store only", "in store only", "see store availability",
    "rupture de stock", "épuisé", "indisponible"
]
_BTN_TEXT_PATTERNS = [
    "add to cart", "ajouter au panier",
    "add to bag", "add to basket"
]

def _text_has_any(hay: str, needles: list) -> bool:
    hay = (hay or "").lower()
    return any(n in hay for n in needles)

def _get_label_text(el: element.Tag) -> str:
    txts = [
        (el.get_text(" ", strip=True) or ""),
        el.get("value") or "",
        el.get("aria-label") or "",
        el.get("title") or "",
        el.get("name") or "",
        el.get("data-action") or "",
        el.get("data-add-to-cart") or "",
        el.get("data-qa") or "",
        el.get("data-oc-click") or "",
        el.get("data-testid") or "",
    ]
    return " ".join(t for t in txts if t).strip().lower()

def _is_element_enabled(el: element.Tag) -> bool:
    if "disabled" in el.attrs: return False
    if (el.get("aria-disabled") or "").lower() in ("true", "1"): return False
    if (el.get("data-available") or "").lower() in ("false", "0"): return False
    classes = " ".join(el.get("class", [])).lower()
    if any(s in classes for s in ["disabled", "is-disabled", "disabled-button", "soldout"]):
        return False
    style = (el.get("style") or "").replace(" ", "").lower()
    if any(s in style for s in ["display:none", "visibility:hidden", "pointer-events:none", "opacity:0"]):
        return False
    parent = el.parent
    depth = 0
    while parent is not None and depth < 4:
        pstyle = (getattr(parent, "attrs", {}).get("style") or "").replace(" ", "").lower()
        pclass = " ".join(getattr(parent, "attrs", []).get("class", [])).lower() if isinstance(getattr(parent, "attrs", {}).get("class", []), list) else " ".join(getattr(parent, "attrs", {}).get("class", [])).lower()
        if any(s in pstyle for s in ["display:none", "visibility:hidden"]) or \
           any(s in pclass for s in ["d-none", "hidden", "visually-hidden"]):
            return False
        parent = parent.parent
        depth += 1
    return True

def _parse_jsonld_availability(soup: BeautifulSoup):
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.text or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        def scan(node):
            vals = []
            if isinstance(node, dict):
                if "availability" in node:
                    vals.append(str(node["availability"]).lower())
                if "offers" in node:
                    vals += scan(node["offers"])
                for v in node.values():
                    vals += scan(v)
            elif isinstance(node, list):
                for it in node:
                    vals += scan(it)
            return vals

        vals = scan(data)
        if vals:
            if any("instock" in v for v in vals):
                return True
            if any("outofstock" in v for v in vals):
                return False
    return None

def _parse_microdata_availability(soup: BeautifulSoup):
    for tag in soup.select('[itemprop="availability"]'):
        val = (tag.get("href") or tag.get("content") or tag.get_text()).lower()
        if "instock" in val:
            return True
        if "outofstock" in val:
            return False
    return None

def _has_add_to_cart(soup: BeautifulSoup) -> bool:
    candidates = soup.select(
        "button, a[role='button'], input[type='submit'], "
        "[data-qa*='add-to-cart' i], [data-oc-click*='addlineitem' i]"
    )
    for el in candidates:
        label = _get_label_text(el)
        if any(pat in label for pat in _BTN_TEXT_PATTERNS) or \
           ("add-to-cart" in label) or ("addlineitem" in label):
            if _is_element_enabled(el):
                return True
    raw = soup.decode().lower()
    if ("product-add-to-cart" in raw or "addlineitem" in raw) and DEBUG:
        print("[sportsexperts][DEBUG] 源码含 'product-add-to-cart' 或 'addLineItem'，但未命中选择器/可见规则", flush=True)
    return False

# ===== Sports Experts 库存检测 =====
def check_stock_sportsexperts(url: str) -> bool:
    html = http_get(url)
    soup = BeautifulSoup(html, "html.parser")

    if DEBUG:
        _debug_save_html("sportsexperts", "page", url, html)

    # 若是防护页，直接提示并返回 False（线上不可判定）
    if _is_incapsula_block(html):
        print("[sportsexperts] 被 Incapsula 拦截，无法获取真实页面（考虑注入 Cookie 或用浏览器渲染）", flush=True)
        return False

    # 先看 Add to Cart（最可靠）
    if _has_add_to_cart(soup):
        if DEBUG: print("[sportsexperts] 检出可点击的 Add to Cart => True", flush=True)
        return True

    # JSON/Microdata：只把 InStock 当真
    avail = _parse_jsonld_availability(soup)
    if avail is True:
        if DEBUG: print("[sportsexperts] availability(JSON-LD)=InStock => True", flush=True)
        return True
    avail2 = _parse_microdata_availability(soup)
    if avail2 is True:
        if DEBUG: print("[sportsexperts] availability(microdata)=InStock => True", flush=True)
        return True

    # 明确“仅门店/线下可购”的提示
    plain = soup.get_text(" ", strip=True).lower()
    if "in-store only" in plain or "in store only" in plain:
        if DEBUG: print("[sportsexperts] 检测到 In-Store Only（仅门店可买）=> False(线上)", flush=True)
        return False
    if "see store availability" in plain:
        if DEBUG: print("[sportsexperts] 检测到 See store availability（门店库存查询）=> False(线上)", flush=True)
        return False

    # 其他无货文案
    if _text_has_any(plain, _AVAIL_NEG_PATTERNS):
        if DEBUG: print("[sportsexperts] 文案包含无货提示 => False", flush=True)
        return False

    if DEBUG: print("[sportsexperts] 未发现明确有货信号 => False", flush=True)
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
