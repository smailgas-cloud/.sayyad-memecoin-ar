import os, time, threading, requests
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
API = "https://api.dexscreener.com"
STATE = {
    "mode": "paper",
    "starting_balance": 0.0,
    "balance": 0.0,
    "positions": {},
    "trades": [],
    "scanned": [],
    "last_scan": None,
    "running": False,
    "settings": {"min_liquidity": 10000, "max_age_hours": 24, "buy_score": 70, "trade_size": 25, "take_profit": 20, "stop_loss": 12}
}
LOCK = threading.Lock()

def num(v, default=0):
    try: return float(v or 0)
    except (ValueError, TypeError): return default

def get_candidates():
    # DexScreener public endpoint: latest token profiles; then fetch Solana pairs for each token.
    r = requests.get(f"{API}/token-profiles/latest/v1", timeout=15)
    r.raise_for_status()
    profiles = r.json()
    out, seen = [], set()
    for p in profiles[:100]:
        if p.get("chainId") != "solana":
            continue
        addr = p.get("tokenAddress")
        if not addr or addr in seen: continue
        seen.add(addr)
        try:
            rr = requests.get(f"{API}/token-pairs/v1/solana/{addr}", timeout=10)
            if not rr.ok: continue
            pairs = rr.json()
            if not isinstance(pairs, list) or not pairs: continue
            pair = max(pairs, key=lambda x: num((x.get("liquidity") or {}).get("usd")))
            liq = num((pair.get("liquidity") or {}).get("usd"))
            vol = num((pair.get("volume") or {}).get("h24"))
            buys = num(((pair.get("txns") or {}).get("h1") or {}).get("buys"))
            sells = num(((pair.get("txns") or {}).get("h1") or {}).get("sells"))
            age_hours = max(0, (time.time()*1000 - num(pair.get("pairCreatedAt"))) / 3600000) if pair.get("pairCreatedAt") else 99999
            change = num((pair.get("priceChange") or {}).get("h1"))
            score = 0
            if liq >= 10000: score += 30
            elif liq >= 5000: score += 18
            if vol >= 20000: score += 20
            elif vol >= 5000: score += 12
            if buys + sells >= 20 and buys > sells: score += 15
            if -10 <= change <= 35: score += 15
            if age_hours <= 24: score += 10
            if liq < 3000: score -= 30
            if change > 80: score -= 20
            if change < -35: score -= 20
            out.append({
                "name": (pair.get("baseToken") or {}).get("name", p.get("description") or "Unknown"),
                "symbol": (pair.get("baseToken") or {}).get("symbol", "?"),
                "address": addr, "price": num(pair.get("priceUsd")),
                "liquidity": liq, "volume24h": vol, "change1h": change,
                "buys1h": int(buys), "sells1h": int(sells),
                "ageHours": round(age_hours, 1), "score": max(0, min(100, score)),
                "url": pair.get("url", f"https://dexscreener.com/solana/{addr}"),
                "warning": "تقييم آلي أولي فقط؛ لا يثبت سلامة العملة"
            })
        except Exception:
            continue
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:50]

def scan_worker():
    while True:
        with LOCK:
            running = STATE["running"]
        if running:
            try:
                candidates = get_candidates()
                with LOCK:
                    STATE["scanned"] = candidates
                    STATE["last_scan"] = datetime.now(timezone.utc).isoformat()
                    # Paper trading only. Never sends a blockchain transaction.
                    settings = STATE["settings"]
                    for c in candidates:
                        if c["score"] >= settings["buy_score"] and c["liquidity"] >= settings["min_liquidity"] and c["ageHours"] <= settings["max_age_hours"]:
                            if c["address"] not in STATE["positions"] and STATE["balance"] >= settings["trade_size"]:
                                qty = settings["trade_size"] / c["price"] if c["price"] > 0 else 0
                                if qty:
                                    STATE["balance"] -= settings["trade_size"]
                                    STATE["positions"][c["address"]] = {"name": c["name"], "symbol": c["symbol"], "address": c["address"], "entry": c["price"], "qty": qty, "cost": settings["trade_size"], "url": c["url"]}
                                    STATE["trades"].insert(0, {"time": datetime.now(timezone.utc).isoformat(), "side": "BUY (تجريبي)", "symbol": c["symbol"], "price": c["price"], "amount": settings["trade_size"], "pnl": 0})
                    # Update positions and trigger paper exits
                    by_addr = {c["address"]: c for c in candidates}
                    for addr, pos in list(STATE["positions"].items()):
                        c = by_addr.get(addr)
                        if not c or c["price"] <= 0: continue
                        pnl_pct = (c["price"] / pos["entry"] - 1) * 100
                        if pnl_pct >= settings["take_profit"] or pnl_pct <= -settings["stop_loss"]:
                            proceeds = pos["qty"] * c["price"]
                            pnl = proceeds - pos["cost"]
                            STATE["balance"] += proceeds
                            STATE["trades"].insert(0, {"time": datetime.now(timezone.utc).isoformat(), "side": "SELL (تجريبي)", "symbol": pos["symbol"], "price": c["price"], "amount": proceeds, "pnl": pnl})
                            del STATE["positions"][addr]
            except Exception as e:
                with LOCK:
                    STATE["last_error"] = str(e)
        time.sleep(60)

threading.Thread(target=scan_worker, daemon=True).start()

@app.route("/")
def home(): return render_template("index.html")

@app.route("/api/state")
def state():
    with LOCK:
        positions = list(STATE["positions"].values())
        return jsonify({"mode": STATE["mode"], "balance": STATE["balance"], "startingBalance": STATE["starting_balance"],
                        "positions": positions, "trades": STATE["trades"][:50], "scanned": STATE["scanned"],
                        "lastScan": STATE["last_scan"], "running": STATE["running"], "settings": STATE["settings"],
                        "lastError": STATE.get("last_error")})

@app.post("/api/start")
def start():
    with LOCK:
        if STATE["starting_balance"] <= 0:
            return jsonify({"ok": False, "error": "أدخل رأس مال افتراضيًا أكبر من صفر لبدء محاكاة الصفقات."}), 400
        STATE["running"] = True
    return jsonify({"ok": True})

@app.post("/api/stop")
def stop():
    with LOCK: STATE["running"] = False
    return jsonify({"ok": True})

@app.post("/api/reset")
def reset():
    data = request.get_json(silent=True) or {}
    amount = max(0, min(num(data.get("amount")), 1000000))
    with LOCK:
        STATE["starting_balance"] = amount
        STATE["balance"] = amount
        STATE["positions"] = {}
        STATE["trades"] = []
        STATE["scanned"] = []
        STATE["last_error"] = None
        STATE["running"] = False
    return jsonify({"ok": True})

@app.post("/api/settings")
def settings():
    data = request.get_json(silent=True) or {}
    with LOCK:
        for k in ["min_liquidity", "max_age_hours", "buy_score", "trade_size", "take_profit", "stop_loss"]:
            if k in data: STATE["settings"][k] = max(1, min(num(data[k]), 1000000))
    return jsonify({"ok": True})

@app.post("/api/scan")
def scan_now():
    try:
        candidates = get_candidates()
        with LOCK:
            STATE["scanned"] = candidates
            STATE["last_scan"] = datetime.now(timezone.utc).isoformat()
        return jsonify({"ok": True, "count": len(candidates)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
