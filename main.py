import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup, element

# ===== 环境变量 =====
WEBHOOK_URL     = os.getenv("DISCORD_WEBHOOK_URL")          # Railway 配置
INTERVAL_SEC    = int(os.getenv("INTERVAL_SEC", "600"))     # 轮询间隔（秒）
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))   # 请求超时（秒）
DEBUG           = os.getenv("DEBUG", "0") == "1"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/123.0.0.0 Safari/537.36"),
    "Accept-Language": "en,zh-CN;q=0.9,zh;q=0.8",
}

# ===== 商品清单 =====
PRODUCTS = [
    # ----- trailhead -----
    {
        "site": "trailhead",
        "name": "Arc'teryx Covert Cardigan Men's",
        "url": "https://www.trailheadpaddleshack.ca/arcteryx-covert-cardigan-mens.html?id=113476423&quantity=1",
        "color": "Cloud Heather / Void",
        "sizes": ["S", "M", "L"],
    },
    {
        "site": "trailhead",
        "name": "Arc'teryx Gamma MX Hoody Men's",
        "url": "https://www.trailheadpaddleshack.ca/arcteryx-gamma-mx-hoody-mens.html",
        "color": "Black",
        "sizes": ["M", "L"],
    },
    {
        "site": "trailhead",
        "name": "Arc'teryx Rho LT Zip Neck Top Men's",
        "url": "https://www.trailheadpaddleshack.ca/arcteryx-rho-lt-zip-neck-top-mens.html",
        "color": "Black",
        "sizes": ["S", "M", "L", "XL", "XXL"],
    },
    {
        "site": "trailhead",
        "name": "Arc'teryx Heliad 15 Backpack",
        "url": "https://www.trailheadpaddleshack.ca/arcteryx-heliad-15-backpack.html",
        "color": "Black",
        "sizes": [],
    },
    {
        "site": "trailhead",
        "name": "Arc'teryx Heliad 15 Backpack",
        "url": "https://www.trailheadpaddleshack.ca/arcteryx-heliad-15-backpack.html",
        "color": "Stone Green",
        "sizes": [],
    },
    # ----- sports experts -----
    {
        "site": "sportsexperts",
        "name": "Arc'teryx Heliad 15 Backpack",
        "url": "https://www.sportsexperts.ca/en-CA/p-heliad-15-compressible-backpack/435066/435066-1",
        "color": "Black",
        "sizes": [],
    },
    {
        "site": "sportsexperts",
        "name": "Arc'teryx Heliad Shoulder Bag",
        "url": "https://www.sportsexperts.ca/en-CA/p-heliad-shoulder-bag/435067/435067-1",
        "color": "Black",
        "sizes": [],
    },
     {
        "site": "sportsexperts",
        "name": "Arc'teryx Rho Zip Neck Men's Baselayer Long-Sleeved Shirt",
        "url": "https://www.sportsexperts.ca/en-CA/p-rho-zipneck-mens-baselayer-long-sleeved-shirt/230173/230173-1",
        "color": "Black",
        "sizes": [],   # 不用管尺码，任意有 Add to Cart 就算有货
    },
         {
        "site": "sportsexperts",
        "name": "Arc'teryx Rho Zip Neck - Women's Baselayer Long-Sleeved Shirt",
        "url": "https://www.sportsexperts.ca/en-CA/p-rho-zipneck-womens-baselayer-long-sleeved-shirt/668324/668324-16",
        "color": "Black",
        "sizes": [],   # 不用管尺码，任意有 Add to Cart 就算有货
    },
]

# ===== 工具：HTTP =====
def http_get(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text

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

# ===== 辅助：Sportsexperts 解析 =====
_AVAIL_NEG_PATTERNS = [
    "sold out", "out of stock", "currently unavailable",
    "rupture de stock", "épuisé", "indisponible"  # 法语兜底
]
_BTN_TEXT_PATTERNS = [
    "add to cart", "ajouter au panier",
    "add to bag", "add to basket"
]

def _text_has_any(hay: str, needles: list) -> bool:
    hay = (hay or "").lower()
    return any(n in hay for n in needles)

def _is_button_enabled(btn: element.Tag) -> bool:
    # 属性禁用
    if "disabled" in btn.attrs: return False
    if (btn.get("aria-disabled") or "").lower() in ("true", "1"): return False
    if (btn.get("data-available") or "").lower() in ("false", "0"): return False
    # class 禁用/隐藏
    classes = " ".join(btn.get("class", [])).lower()
    if any(s in classes for s in ["disabled", "is-disabled", "disabled-button", "soldout"]):
        return False
    # 样式隐藏
    style = (btn.get("style") or "").replace(" ", "").lower()
    if any(s in style for s in ["display:none", "visibility:hidden", "pointer-events:none"]):
        return False
    # 祖先隐藏（简单检查）
    parent = btn.parent
    depth = 0
    while parent and depth < 4:
        pstyle = (getattr(parent, "attrs", {}).get("style") or "").replace(" ", "").lower()
        pclass = " ".join(getattr(parent, "attrs", {}).get("class", [])).lower()
        if any(s in pstyle for s in ["display:none", "visibility:hidden"]) or \
           any(s in pclass for s in ["d-none", "hidden", "visually-hidden"]):
            return False
        parent = parent.parent
        depth += 1
    return True

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
    """从 microdata/link/meta 读取 availability。返回 True/False/None"""
    for tag in soup.select('[itemprop="availability"]'):
        val = (tag.get("href") or tag.get("content") or tag.get_text()).lower()
        if "instock" in val:
            return True
        if "outofstock" in val:
            return False
    return None

# ===== Sports Experts 库存检测 =====
def check_stock_sportsexperts(url: str) -> bool:
    """
    判定次序：
      1) JSON-LD / microdata 的 availability
      2) 可见且未禁用的“add to cart / ajouter au panier / add to bag / add to basket”按钮
      3) 页面出现 Sold out / Out of stock 等字样 -> 无货
      4) 其他情况保守返回 False（无货）
    """
    html = http_get(url)
    soup = BeautifulSoup(html, "html.parser")

    # 1) 结构化数据（最可靠）
    avail = _parse_jsonld_availability(soup)
    if avail is None:
        avail = _parse_microdata_availability(soup)
    if avail is not None:
        if DEBUG: print(f"[sportsexperts] availability(JSON/microdata) => {avail}", flush=True)
        return bool(avail)

    # 2) 严格按钮判定：必须含“add to …”文案，且可见/未禁用
    btn_candidates = []
    for btn in soup.find_all("button"):
        label = (btn.get_text(" ", strip=True) or "").lower()
        if _text_has_any(label, _BTN_TEXT_PATTERNS):
            btn_candidates.append(btn)

    for btn in btn_candidates:
        if _is_button_enabled(btn):
            if DEBUG: print("[sportsexperts] 按钮存在且可点击 => True", flush=True)
            return True

    # 3) 文案兜底：常见“无货”提示
    plain = soup.get_text(" ", strip=True).lower()
    if _text_has_any(plain, _AVAIL_NEG_PATTERNS):
        if DEBUG: print("[sportsexperts] 文案包含无货提示 => False", flush=True)
        return False

    # 4) 保守处理：视为无货
    if DEBUG: print("[sportsexperts] 未识别到明确有货信号 => False", flush=True)
    return False

# ===== 主循环 =====
if __name__ == "__main__":
    print("开始监控多个商品库存状态...", flush=True)
    # 关键：用 (site, name, color) 作为键，避免站点之间撞键
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
