import os
import time
import requests
from bs4 import BeautifulSoup, element

# ----- 环境变量 -----
WEBHOOK_URL   = os.getenv("DISCORD_WEBHOOK_URL")         # 在 Railway 里配置
INTERVAL_SEC  = int(os.getenv("INTERVAL_SEC", "600"))    # 可选：轮询间隔（秒），默认10分钟
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))# 可选：请求超时（秒）

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
}

# ----- 商品清单 -----
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
]

# ----- 工具：HTTP -----
def http_get(url: str) -> str:
    """GET 页面并返回文本，统一 headers 与超时设置。"""
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text

# ----- Discord 发送 -----
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

# ----- Trailhead 库存检测 -----
def check_stock_trailhead(url: str, color: str, sizes: list):
    """
    当 sizes 为空：只判断该颜色是否可选（__any__）
    返回：
      - 有尺码：{size: bool, ...}
      - 无尺码：{"__any__": bool}
    """
    html = http_get(url)
    soup = BeautifulSoup(html, "html.parser")

    # Trailhead 的规格选择一般在 <select id="prodattr2"> 上
    select = soup.find("select", id="prodattr2")
    if not select:
        # 没有选择器时按“不可选”处理，避免返回空 dict 导致上层混淆
        return {"__any__": False} if not sizes else {s: False for s in sizes}

    options = select.find_all("option")

    # 无尺码：只看该颜色是否有可选项（未 disabled）
    if not sizes:
        available = any(
            (opt.get("data-color", "").strip() == color) and (not opt.has_attr("disabled"))
            for opt in options
        )
        return {"__any__": available}

    # 有尺码：汇总每个尺码的可用性
    stock_status = {size: False for size in sizes}
    for opt in options:
        if opt.get("data-color", "").strip() == color:
            opt_size = opt.get("data-size", "").strip()
            if opt_size in stock_status:
                stock_status[opt_size] = not opt.has_attr("disabled")
    return stock_status

# ----- Sports Experts 库存检测 -----
def check_stock_sportsexperts(url: str) -> bool:
    """
    Sports Experts：出现可点击的“ADD TO CART”视为有货。
    做了更稳健的按钮/属性检查。
    """
    html = http_get(url)
    soup = BeautifulSoup(html, "html.parser")

    # 先找文字匹配“add to cart”的按钮
    btn = soup.find("button", string=lambda s: isinstance(s, str) and "add to cart" in s.lower())

    # 若未命中，再尝试常见选择器
    if btn is None:
        for sel in [
            'button[name="add"]',
            'button[type="submit"]',
            "button.add-to-cart",
            "button#AddToCart-product-template",
        ]:
            candidate = soup.select_one(sel)
            if isinstance(candidate, element.Tag):
                btn = candidate
                break

    # 未发现按钮：很可能无货或静态占位
    if not isinstance(btn, element.Tag):
        # 再从页面文案兜底（有些模板用 Sold Out 提示）
        text = soup.get_text(" ", strip=True).lower()
        if any(x in text for x in ["sold out", "out of stock", "currently unavailable"]):
            return False
        return False

    # 有按钮：判断是否禁用
    disabled = (
        ("disabled" in btn.attrs) or
        (btn.get("aria-disabled") in ["true", "1"]) or
        (btn.get("data-available") in ["false", "0"])
    )
    return not disabled

# ----- 主循环 -----
if __name__ == "__main__":
    print("开始监控多个商品库存状态...", flush=True)
    # 关键修复：把键改成 (site, name, color)，避免不同站点同名同色撞键
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

                    else:      # 无尺码：使用 __any__
                        available = bool(current_status.get("__any__", False))
                        # 历史值可能不存在或旧格式，这里做类型防御
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
                    last_status = last_status_all.get(key)  # 这里的 last_status 是 bool 或 None
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