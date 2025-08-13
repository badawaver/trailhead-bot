import os
import time
import requests
from bs4 import BeautifulSoup

# ----- 环境变量 -----
WEBHOOK_URL   = os.getenv("DISCORD_WEBHOOK_URL")   # 在 Railway 里配置
INTERVAL_SEC  = int(os.getenv("INTERVAL_SEC", "600"))  # 可选：轮询间隔（秒），默认10分钟

# ----- 你的商品清单 -----
PRODUCTS = [
    # trailhead 示例
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
    # sports experts 示例
    {
        "site": "sportsexperts",
        "name": "Arc'teryx Heliad 15 Backpack",
        "url": "https://www.sportsexperts.ca/en-CA/p-heliad-15-compressible-backpack/435066/435066-1",
        "color": "Black",
        "sizes": [],  # 不需要用到 sizes
    },
]

# ----- Discord 发送 -----
def send_discord_message(text: str):
    if not WEBHOOK_URL:
        print("WARN: 未设置 DISCORD_WEBHOOK_URL，跳过发送", flush=True)
        return
    while text:
        chunk = text[:1900]
        text = text[1900:]
        r = requests.post(WEBHOOK_URL, json={"content": chunk}, timeout=15)
        if r.status_code not in (200, 204):
            print("Discord 发送失败:", r.status_code, r.text, flush=True)

# ----- Trailhead 库存检测 -----
def check_stock_multiple_sizes(url, color, sizes):
    """当 sizes 为空：只判断该颜色是否可选"""
    headers = {"User-Agent": "Mozilla/5.0"}
    res = requests.get(url, headers=headers, timeout=30)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    select = soup.find("select", id="prodattr2")
    if not select:
        return {}

    options = select.find_all("option")

    if not sizes:  # 无尺码
        available = any(
            (opt.get("data-color", "") == color) and (not opt.has_attr("disabled"))
            for opt in options
        )
        return {"__any__": available}

    stock_status = {size: False for size in sizes}
    for option in options:
        if option.get("data-color", "") == color:
            opt_size = option.get("data-size", "")
            if opt_size in stock_status:
                stock_status[opt_size] = not option.has_attr("disabled")
    return stock_status

# ----- Sports Experts 库存检测 -----
def check_stock_sportsexperts(url):
    """
    检查 Sports Experts 商品是否有库存
    条件：页面出现 'ADD TO CART' 按钮
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    res = requests.get(url, headers=headers, timeout=30)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")
    button = soup.find("button", string=lambda s: s and "ADD TO CART" in s.upper())
    return bool(button)

# ----- 主循环 -----
if __name__ == "__main__":
    print("开始监控多个商品库存状态...", flush=True)
    last_status_all = {}

    while True:
        all_messages = []
        for product in PRODUCTS:
            name = product["name"]
            url = product["url"]
            color = product["color"]
            sizes = product["sizes"]
            site = product["site"]

            try:
                if site == "trailhead":
                    current_status = check_stock_multiple_sizes(url, color, sizes)
                    if not current_status:
                        continue
                    last_status = last_status_all.get((name, color), {})

                    if sizes:  # 有尺码
                        if current_status != last_status:
                            in_stock = [s for s, stock in current_status.items() if stock]
                            out_stock = [s for s, stock in current_status.items() if not stock]
                            msg = f"trailhead {name} - {color}\n"
                            if in_stock:
                                msg += "✅ 有库存: " + ", ".join(in_stock) + "\n"
                            if out_stock:
                                msg += "❌ 无库存: " + ", ".join(out_stock)
                            all_messages.append(msg)
                            last_status_all[(name, color)] = current_status
                    else:  # 无尺码
                        available = current_status.get("__any__", False)
                        if available != last_status.get("__any__", None):
                            msg = f"trailhead {name} - {color}\n"
                            msg += "✅ 有库存" if available else "❌ 无库存"
                            all_messages.append(msg)
                            last_status_all[(name, color)] = current_status

                elif site == "sportsexperts":
                    in_stock = check_stock_sportsexperts(url)
                    last_status = last_status_all.get((name, color))
                    if in_stock != last_status:
                        msg = f"sportsexperts {name} - {color}\n"
                        msg += "✅ 有库存" if in_stock else "❌ 无库存"
                        all_messages.append(msg)
                        last_status_all[(name, color)] = in_stock

            except Exception as e:
                print(f"请求失败 {site} {name} - {color}: {e}", flush=True)
                continue

        if all_messages:
            send_discord_message("\n\n".join(all_messages))

        time.sleep(INTERVAL_SEC)