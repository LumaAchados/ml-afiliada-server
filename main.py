import os
import json
import time
import threading
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

@app.route("/", methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def handle_options(path=""):
    return "", 204

# ─── Storage (JSON file as simple database) ───────────────────────────────────
DATA_FILE = "data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"criteria": [], "price_history": {}, "alerts": []}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ─── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"Telegram error: {e}")

# ─── ML Search ────────────────────────────────────────────────────────────────
def search_ml(query="", category="", max_price=None, min_discount=None):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        # Use ML search page directly
        search_term = query.replace(" ", "-") if query else ""
        if search_term:
            url = f"https://lista.mercadolivre.com.br/{search_term}"
            if category:
                url = f"https://lista.mercadolivre.com.br/{search_term}_Categoria_{category}"
        elif category:
            url = f"https://www.mercadolivre.com.br/c/{category}"
        else:
            return []

        res = requests.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(res.text, "html.parser")

        results = []
        items = soup.select(".ui-search-layout__item")[:20]

        for item in items:
            try:
                title_el = item.select_one(".poly-component__title")
                price_el = item.select_one(".andes-money-amount__fraction")
                original_el = item.select_one(".andes-money-amount--previous .andes-money-amount__fraction")
                link_el = item.select_one("a.poly-component__title")
                img_el = item.select_one("img.poly-component__picture")
                shipping_el = item.select_one(".poly-component__shipping")

                if not title_el or not price_el:
                    continue

                price_text = price_el.get_text().replace(".", "").replace(",", ".")
                price = float(price_text) if price_text else 0
                if price == 0:
                    continue

                original_price = None
                if original_el:
                    orig_text = original_el.get_text().replace(".", "").replace(",", ".")
                    try:
                        original_price = float(orig_text)
                    except:
                        pass

                discount = 0
                if original_price and original_price > price:
                    discount = round(((original_price - price) / original_price) * 100)

                if max_price and price > float(max_price):
                    continue
                if min_discount and float(min_discount) > 0 and discount < float(min_discount):
                    continue

                product_id = link_el["href"].split("MLB")[1].split("-")[0] if link_el and "MLB" in link_el.get("href", "") else str(int(time.time()))
                thumbnail = img_el.get("src") or img_el.get("data-src") or "" if img_el else ""
                free_shipping = bool(shipping_el and "grátis" in shipping_el.get_text().lower())

                results.append({
                    "id": f"MLB{product_id}",
                    "title": title_el.get_text(strip=True),
                    "price": price,
                    "original_price": original_price,
                    "discount": discount,
                    "thumbnail": thumbnail,
                    "permalink": link_el["href"] if link_el else "",
                    "free_shipping": free_shipping,
                    "reviews": 0,
                })
            except Exception as e:
                print(f"Item parse error: {e}")
                continue

        return results
    except Exception as e:
        print(f"ML search error: {e}")
        return []

def calc_score(product):
    score = 0
    score += min(product.get("discount", 0) * 1.5, 50)
    if product.get("free_shipping"):
        score += 20
    if product.get("reviews", 0) >= 4.5:
        score += 15
    elif product.get("reviews", 0) >= 4:
        score += 8
    if product["price"] <= 100:
        score += 10
    elif product["price"] <= 500:
        score += 5
    return min(round(score), 100)

# ─── Monitor Loop ─────────────────────────────────────────────────────────────
def monitor_loop():
    while True:
        try:
            data = load_data()
            for criterion in data["criteria"]:
                products = search_ml(
                    query=criterion.get("query", ""),
                    category=criterion.get("category", ""),
                    max_price=criterion.get("maxPrice"),
                    min_discount=criterion.get("minDiscount"),
                )
                for product in products:
                    pid = product["id"]
                    history = data["price_history"].get(pid, [])
                    current_price = product["price"]
                    now = datetime.now().isoformat()

                    # Check for price drop
                    if history:
                        last_price = history[-1]["price"]
                        if current_price < last_price:
                            drop_pct = round(((last_price - current_price) / last_price) * 100)
                            alert = {
                                "id": int(time.time()),
                                "product_id": pid,
                                "title": product["title"],
                                "old_price": last_price,
                                "new_price": current_price,
                                "drop_pct": drop_pct,
                                "permalink": product["permalink"],
                                "timestamp": now,
                            }
                            data["alerts"].insert(0, alert)
                            data["alerts"] = data["alerts"][:50]  # keep last 50

                            # Send Telegram notification
                            msg = (
                                f"📉 <b>QUEDA DE PREÇO!</b>\n\n"
                                f"🛍️ {product['title'][:80]}\n"
                                f"💰 De R${last_price:.2f} → R${current_price:.2f} (-{drop_pct}%)\n"
                                f"🔗 {product['permalink']}"
                            )
                            send_telegram(msg)

                    # Save price to history
                    history.append({"price": current_price, "timestamp": now})
                    history = history[-30:]  # keep last 30 entries per product
                    data["price_history"][pid] = history

            save_data(data)
            print(f"[{datetime.now().strftime('%H:%M')}] Monitor check completed.")
        except Exception as e:
            print(f"Monitor error: {e}")

        time.sleep(3600)  # check every hour

# ─── API Routes ───────────────────────────────────────────────────────────────
@app.route("/debug", methods=["GET"])
def debug():
    try:
        url = "https://api.mercadolibre.com/sites/MLB/search?q=iphone+15&limit=3"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": "https://www.mercadolivre.com.br/",
        }
        res = requests.get(url, headers=headers, timeout=15)
        data = res.json()
        return jsonify({
            "status": "ok",
            "ml_status_code": res.status_code,
            "total_results": data.get("paging", {}).get("total", 0),
            "first_result": data.get("results", [{}])[0].get("title", "none") if data.get("results") else "no results"
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "ML Afiliada Server rodando!"})

@app.route("/criteria", methods=["GET"])
def get_criteria():
    data = load_data()
    return jsonify(data["criteria"])

@app.route("/criteria", methods=["POST"])
def add_criterion():
    data = load_data()
    criterion = request.json
    criterion["id"] = int(time.time())
    data["criteria"].append(criterion)
    save_data(data)
    return jsonify(criterion)

@app.route("/criteria/<int:cid>", methods=["DELETE"])
def delete_criterion(cid):
    data = load_data()
    data["criteria"] = [c for c in data["criteria"] if c.get("id") != cid]
    save_data(data)
    return jsonify({"ok": True})

@app.route("/products", methods=["GET"])
def get_products():
    data = load_data()
    all_products = []
    seen = set()
    for criterion in data["criteria"]:
        products = search_ml(
            query=criterion.get("query", ""),
            category=criterion.get("category", ""),
            max_price=criterion.get("maxPrice"),
            min_discount=criterion.get("minDiscount") if float(criterion.get("minDiscount") or 0) > 0 else None,
        )
        for p in products:
            if p["id"] not in seen:
                seen.add(p["id"])
                p["score"] = calc_score(p)
                p["criteriaLabel"] = criterion.get("query") or criterion.get("category", "Busca")
                p["priceHistory"] = data["price_history"].get(p["id"], [])
                all_products.append(p)
    all_products.sort(key=lambda x: x["score"], reverse=True)
    return jsonify(all_products[:20])

@app.route("/alerts", methods=["GET"])
def get_alerts():
    data = load_data()
    return jsonify(data["alerts"][:20])

@app.route("/alerts/<int:aid>", methods=["DELETE"])
def dismiss_alert(aid):
    data = load_data()
    data["alerts"] = [a for a in data["alerts"] if a.get("id") != aid]
    save_data(data)
    return jsonify({"ok": True})

@app.route("/history/<product_id>", methods=["GET"])
def get_history(product_id):
    data = load_data()
    return jsonify(data["price_history"].get(product_id, []))

# ─── Start ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Start background monitor thread
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
