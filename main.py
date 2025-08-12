import requests
from bs4 import BeautifulSoup
import time
import os

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

PRODUCTS = [
    {
        "name": "Arc'teryx Covert Cardigan Men's",
        "url": "https://www.trailheadpaddleshack.ca/arcteryx-covert-cardigan-mens.html?id=113476423&quantity=1",
        "color": "Cloud Heather / Void",
        "sizes": ["S", "M", "L"],
    },
    {
        "name": "Arc'teryx Gamma MX Hoody Men's",
        "url": "https://www.trailheadpaddleshack.ca/arcteryx-gamma-mx-hoody-mens.html",
        "color": "Black",
        "sizes": ["M", "L"],
    },
    {
        "name": "Arc'teryx Rho LT Zip Neck Top Men's",
        "url": "https://www.trailheadpaddleshack.ca/arcteryx-rho-lt-zip-neck-top-mens.html",
        "color": "Black",
        "sizes": ["S", "M", "L", "XL", "XXL"],
    },
    # ===== 新增：Heliad 15 两个颜色（不区分尺码） =====
    {
        "name": "Arc'teryx Heliad 15 Backpack",
        "url": "https://www.trailheadpaddleshack.ca/arcteryx-heliad-15-backpack.html",
        "color": "Black",
        "sizes": [],   # 空列表表示“无尺码”
    },
    {
        "name": "Arc'teryx Heliad 15 Backpack",
        "url": "https://www.trailheadpaddleshack.ca/arcteryx-heliad-15-backpack.html",
        "color": "Stone Green",
        "sizes": [],   # 空列表表示“无尺码”
    },
]

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    requests.post(url, json=payload)

def check_stock_multiple_sizes(url, color, sizes):
    """当 sizes 为空时：只判断该颜色是否可选，返回 {'__any__': True/False}"""
    headers = {"User-Agent": "Mozilla/5.0"}
    res = requests.get(url, headers=headers, timeout=30)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    select = soup.find("select", id="prodattr2")
    if not select:
        return {}

    options = select.find_all("option")

    # 无尺码：只看该 color 的任一 option 是否可选
    if not sizes:  # [] 或 None
        available = False
        for option in options:
            opt_color = option.get("data-color", "")
            disabled = option.has_attr("disabled")
            if opt_color == color and not disabled:
                available = True
                break
        return {"__any__": available}

    # 有尺码：逐尺码判断
    stock_status = {size: False for size in sizes}
    for option in options:
        opt_color = option.get("data-color", "")
        opt_size = option.get("data-size", "")
        disabled = option.has_attr("disabled")
        if opt_color == color and opt_size in stock_status:
            stock_status[opt_size] = not disabled
    return stock_status

if __name__ == "__main__":
    print("开始监控多个商品库存状态...")
    last_status_all = {}

    while True:
        all_messages = []
        for product in PRODUCTS:
            name = product["name"]
            url = product["url"]
            color = product["color"]
            sizes = product["sizes"]

            try:
                current_status = check_stock_multiple_sizes(url, color, sizes)
            except Exception as e:
                print(f"请求失败 {name} - {color}: {e}")
                continue

            if not current_status:
                continue

            last_status = last_status_all.get((name, color), {})

            # 组装消息
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
            else:      # 无尺码（只看颜色是否可选）
                available = current_status.get("__any__", False)
                if available != last_status.get("__any__", None):
                    msg = f"trailhead {name} - {color}\n"
                    msg += "✅ 有库存" if available else "❌ 无库存"
                    all_messages.append(msg)
                    last_status_all[(name, color)] = current_status

        if all_messages:
            send_telegram_message("\n\n".join(all_messages))

        time.sleep(600)