from datetime import datetime, time, timedelta
import json
import os
from kiteconnect import KiteConnect
import pandas as pd
import pyotp
import requests

# =====================================================================
# 1. GLOBAL SETTINGS & ETF UNIVERSE
# =====================================================================
INITIAL_CAPITAL = 100000.0
MAX_ACTIVE_SLOTS = 3
MAX_RISK_PER_TRADE_PCT = 0.01 # 1.0% (₹1,000 max loss per trade)
BASE_TARGET_PCT = 0.035 # Leg 1 Target: +3.5%
STOP_LOSS_PCT = 0.025 # Hard Stop-Loss: -2.5%

DB_FILE = "trade_database.json"
MEMORY_FILE = "strategy_memory.json"
HOLIDAYS_CACHE_FILE = "holidays_cache.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

WATCHLIST = {
    "NIFTYBEES": {"token": 256265, "cluster": "BROAD_MARKET"},
    "JUNIORBEES": {"token": 341249, "cluster": "BROAD_MARKET"},
    "BANKBEES": {"token": 260105, "cluster": "BROAD_MARKET"},
    "ITBEES": {"token": 408065, "cluster": "TECH_EXPORT"},
    "AUTOBEES": {"token": 412673, "cluster": "AUTO_CYCLICAL"},
    "PHARMABEES": {"token": 345601, "cluster": "HEALTHCARE_DEFENSIVE"},
    "GOLDBEES": {"token": 367745, "cluster": "COMMODITY_HEDGE"},
}


# =====================================================================
# 2. UTILITY & NOTIFICATION FUNCTIONS
# =====================================================================
def send_telegram(message: str, reply_markup: dict = None):
    """Dispatches a message to Telegram, optionally with interactive inline buttons."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"Notification (Telegram credentials missing):\n{message}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Dispatch Error: {e}")


def load_json(filepath: str, default_val):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r") as f:
                return json.load(f)
        except Exception:
            return default_val
    return default_val


def save_json(filepath: str, data):
    with open(filepath, "w") as f:
        json.dump(data, f, indent=4)


# =====================================================================
# 3. INTERACTIVE TELEGRAM LISTENER (BUTTONS & /STATUS COMMAND)
# =====================================================================
def process_telegram_updates():
    """Listens for /status command and processes inline button clicks."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        res = requests.get(url, timeout=10).json()
        updates = res.get("result", [])
        if not updates:
            return

        trades = load_json(DB_FILE, [])
        memory = load_json(
            MEMORY_FILE,
            {"stcl_pool": 0.0, "cooldowns": {}, "portfolio_peak": 100000.0},
        )
        active_trades = [t for t in trades if t.get("status") == "OPEN"]
        active_symbols = [t["symbol"] for t in active_trades]

        for item in updates:
            # 1. Handle regular text commands (/status, /dashboard)
            if "message" in item:
                msg = item["message"]
                text = msg.get("text", "").strip()
                if text in ["/status", "/dashboard", "/pnl"]:
                    pos_text = ""
                    if not active_trades:
                        pos_text = "• *Active Slots:* 0/3 (100% Cash)\n"
                    else:
                        for t in active_trades:
                            pos_text += f"• `{t['symbol']}`: {t['units']} units @ ₹{t['entry_price']} (SL: ₹{t['sl']})\n"

                    status_report = (
                        f"📊 *LIVE ENGINE MONITOR*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🟢 *Status:* Active & Polling (15-min intervals)\n"
                        f"⏰ *Server Time:* {datetime.now().strftime('%H:%M:%S IST')}\n"
                        f"💰 *Investable Capital Base:* ₹1,00,000.00\n"
                        f"🛡️ *Tax Shield (STCL Pool):* ₹{memory.get('stcl_pool', 0.0):,.2f}\n\n"
                        f"*Open Positions ({len(active_trades)}/{MAX_ACTIVE_SLOTS}):*\n{pos_text}"
                        f"━━━━━━━━━━━━━━━━━━━━"
                    )
                    send_telegram(status_report)

            # 2. Handle button clicks (Approve / Pass)
            elif "callback_query" in item:
                cb = item["callback_query"]
                cb_id = cb["id"]
                data = cb.get("data", "")
                msg_id = cb["message"]["message_id"]

                # Acknowledge callback immediately
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                    json={"callback_query_id": cb_id},
                )

                if data.startswith("BUY:"):
                    _, sym, entry, qty, stop = data.split(":")
                    entry_p = float(entry)
                    units_val = int(qty)
                    sl_val = float(stop)

                    if sym not in active_symbols:
                        new_trade = {
                            "symbol": sym,
                            "entry_price": entry_p,
                            "units": units_val,
                            "remaining_units": units_val,
                            "sl": sl_val,
                            "entry_date": datetime.now().strftime("%Y-%m-%d"),
                            "status": "OPEN",
                            "leg1_done": False,
                        }
                        trades.append(new_trade)
                        active_symbols.append(sym)
                        save_json(DB_FILE, trades)

                        edit_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
                        requests.post(
                            edit_url,
                            json={
                                "chat_id": TELEGRAM_CHAT_ID,
                                "message_id": msg_id,
                                "text": f"✅ *TRADE APPROVED & LOGGED: {sym}*\n"
                                f"Recorded {units_val} units @ ₹{entry_p} in paper portfolio.",
                                "parse_mode": "Markdown",
                            },
                        )

                elif data.startswith("PASS:"):
                    _, sym = data.split(":")
                    edit_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
                    requests.post(
                        edit_url,
                        json={
                            "chat_id": TELEGRAM_CHAT_ID,
                            "message_id": msg_id,
                            "text": f"❌ *TRADE SKIPPED: {sym}*",
                            "parse_mode": "Markdown",
                        },
                    )

        # Clear processed updates
        last_id = updates[-1]["update_id"]
        requests.get(f"{url}?offset={last_id + 1}")
    except Exception as e:
        print(f"Error handling Telegram updates: {e}")


# =====================================================================
# 4. VISUAL HTML DASHBOARD GENERATOR (GITHUB PAGES)
# =====================================================================
def generate_web_dashboard():
    """Generates a responsive static HTML dashboard in docs/index.html."""
    os.makedirs("docs", exist_ok=True)
    trades = load_json(DB_FILE, [])
    memory = load_json(
        MEMORY_FILE,
        {"stcl_pool": 0.0, "cooldowns": {}, "portfolio_peak": 100000.0},
    )
    active = [t for t in trades if t.get("status") == "OPEN"]
    closed = [t for t in trades if t.get("status") == "CLOSED"]

    active_rows = ""
    if not active:
        active_rows = '<tr><td colspan="4" style="text-align:center; color:#64748b; padding:12px;">No active positions (100% Cash)</td></tr>'
    else:
        for t in active:
            active_rows += f"<tr><td><b>{t['symbol']}</b></td><td>{t['units']}</td><td>₹{t['entry_price']}</td><td>₹{t['sl']}</td></tr>"

    closed_rows = ""
    if not closed:
        closed_rows = '<tr><td colspan="3" style="text-align:center; color:#64748b; padding:12px;">No closed trades logged yet</td></tr>'
    else:
        for t in closed[-5:]:
            closed_rows += f"<tr><td><b>{t['symbol']}</b></td><td>{t.get('exit_reason', 'Closed')}</td><td>{t.get('exit_date', '-')}</td></tr>"

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ETF Trading Engine Monitor</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 16px; }}
        .container {{ max-width: 600px; margin: 0 auto; }}
        h2 {{ font-size: 20px; margin-bottom: 16px; color: #38bdf8; display: flex; align-items: center; justify-content: space-between; }}
        .badge {{ background: #10b981; color: white; padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: bold; }}
        .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }}
        .card {{ background: #1e293b; border-radius: 12px; padding: 14px; border: 1px solid #334155; }}
        .card-label {{ font-size: 12px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; }}
        .card-val {{ font-size: 18px; font-weight: bold; margin-top: 6px; color: #f1f5f9; }}
        .section-card {{ background: #1e293b; border-radius: 12px; padding: 16px; margin-bottom: 16px; border: 1px solid #334155; }}
        .section-title {{ font-size: 15px; font-weight: bold; margin-bottom: 12px; color: #e2e8f0; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        th, td {{ text-align: left; padding: 8px 4px; border-bottom: 1px solid #334155; }}
        th {{ color: #94a3b8; font-weight: 600; font-size: 11px; text-transform: uppercase; }}
        .footer {{ text-align: center; font-size: 11px; color: #64748b; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="container">
        <h2>
            <span>⚡ ETF Swing Engine</span>
            <span class="badge">ONLINE</span>
        </h2>

        <div class="grid">
            <div class="card">
                <div class="card-label">Last Polled</div>
                <div class="card-val" style="font-size: 15px;">{datetime.now().strftime('%H:%M IST')}</div>
            </div>
            <div class="card">
                <div class="card-label">Active Slots</div>
                <div class="card-val">{len(active)} / {MAX_ACTIVE_SLOTS}</div>
            </div>
            <div class="card">
                <div class="card-label">Capital Base</div>
                <div class="card-val">₹1,00,000</div>
            </div>
            <div class="card">
                <div class="card-label">Tax Loss Shield</div>
                <div class="card-val">₹{memory.get('stcl_pool', 0.0):,.2f}</div>
            </div>
        </div>

        <div class="section-card">
            <div class="section-title">Active Positions</div>
            <table>
                <thead>
                    <tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Stop-Loss</th></tr>
                </thead>
                <tbody>
                    {active_rows}
                </tbody>
            </table>
        </div>

        <div class="section-card">
            <div class="section-title">Recent Realized Trades</div>
            <table>
                <thead>
                    <tr><th>Symbol</th><th>Exit Reason</th><th>Date</th></tr>
                </thead>
                <tbody>
                    {closed_rows}
                </tbody>
            </table>
        </div>

        <div class="footer">
            Automated Multi-Agent Engine • Cash Delivery (CNC)
        </div>
    </div>
</body>
</html>"""
    with open("docs/index.html", "w") as f:
        f.write(html_content)


# =====================================================================
# 5. AUTHENTICATION (KITE CONNECT AUTOMATION)
# =====================================================================
def get_kite_session():
    api_key = os.getenv("KITE_API_KEY")
    api_secret = os.getenv("KITE_API_SECRET")
    user_id = os.getenv("KITE_USER_ID")
    password = os.getenv("KITE_PASSWORD")
    totp_secret = os.getenv("KITE_TOTP_SECRET")

    if not all([api_key, api_secret, user_id, password, totp_secret]):
        print("❌ Kite Connect Error: Missing GitHub Secrets.")
        return None

    try:
        kite = KiteConnect(api_key=api_key.strip())
        totp = pyotp.TOTP(totp_secret.strip()).now()
        session = requests.Session()

        # Step 1: User ID + Password
        login_res = session.post(
            "https://kite.zerodha.com/api/login",
            data={"user_id": user_id.strip(), "password": password.strip()},
            timeout=10,
        ).json()

        if login_res.get("status") != "success":
            print(
                f"❌ Step 1 (Login) Failed: {login_res.get('message', 'Check User ID / Password')}"
            )
            return None
        print("✅ Step 1: User ID and Password verified.")

        request_id = login_res["data"]["request_id"]

        # Step 2: TOTP 2FA
        twofa_res = session.post(
            "https://kite.zerodha.com/api/twofa",
            data={
                "user_id": user_id.strip(),
                "request_id": request_id,
                "twofa_value": totp,
                "skip_session": "",
            },
            timeout=10,
        ).json()

        if twofa_res.get("status") != "success":
            print(
                f"❌ Step 2 (2FA) Failed: {twofa_res.get('message', 'Check KITE_TOTP_SECRET key')}"
            )
            return None
        print("✅ Step 2: 2FA TOTP verified.")

        # Step 3: OAuth Token Extraction
        req_url = f"https://kite.zerodha.com/connect/login?api_key={api_key.strip()}&skip_session=true"
        resp = session.get(req_url, timeout=10)

        if "request_token=" not in resp.url:
            print(f"❌ Step 3 Failed: Redirect URL was {resp.url}")
            return None

        request_token = resp.url.split("request_token=")[1].split("&")[0]
        data = kite.generate_session(
            request_token, api_secret=api_secret.strip()
        )
        kite.set_access_token(data["access_token"])
        print("✅ Step 3: Session established. Live data stream connected.")
        return kite

    except Exception as e:
        print(f"❌ Kite Connect Exception: {e}")
        return None


# =====================================================================
# 6. MARKET TIMING & TAX ENGINE
# =====================================================================
def get_trading_holidays() -> set[str]:
    now = datetime.now()
    cache = load_json(HOLIDAYS_CACHE_FILE, {})

    if "last_updated" in cache:
        try:
            last_updated = datetime.strptime(cache["last_updated"], "%Y-%m-%d")
            if (now - last_updated).days < 7:
                return set(cache.get("holidays", []))
        except Exception:
            pass

    try:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
        s = requests.Session()
        s.get("https://www.nseindia.com", headers=headers, timeout=5)
        res = s.get(
            "https://www.nseindia.com/api/holiday-master?type=trading",
            headers=headers,
            timeout=10,
        )
        if res.status_code == 200:
            parsed = [
                datetime.strptime(
                    item["tradingDate"], "%d-%b-%Y"
                ).strftime("%Y-%m-%d")
                for item in res.json().get("CM", [])
                if "tradingDate" in item
            ]
            save_json(
                HOLIDAYS_CACHE_FILE,
                {
                    "last_updated": now.strftime("%Y-%m-%d"),
                    "holidays": parsed,
                },
            )
            return set(parsed)
    except Exception:
        pass
    return set(cache.get("holidays", []))


def is_market_open():
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    current_time = now.time()

    if now.weekday() >= 5 or today_str in get_trading_holidays():
        return False, "Market is closed today (Weekend or NSE Holiday)."
    if not (time(9, 15) <= current_time <= time(15, 30)):
        return False, f"Outside trading hours ({current_time.strftime('%H:%M')} IST)."
    return True, "Market Active"


def compute_net_pnl(buy_price: float, sell_price: float, units: int):
    buy_val = buy_price * units
    sell_val = sell_price * units
    gross_pnl = sell_val - buy_val

    stt = (buy_val + sell_val) * 0.0010
    stamp_duty = buy_val * 0.00015
    exchange_fee = (buy_val + sell_val) * 0.0000297
    sebi_charges = (buy_val + sell_val) * 0.000001
    gst = (exchange_fee + sebi_charges) * 0.18
    dp_charge = 15.34

    total_charges = (
        stt + stamp_duty + exchange_fee + sebi_charges + gst + dp_charge
    )
    net_pre_tax = gross_pnl - total_charges

    memory = load_json(
        MEMORY_FILE,
        {"stcl_pool": 0.0, "cooldowns": {}, "portfolio_peak": INITIAL_CAPITAL},
    )
    stcl_pool = memory.get("stcl_pool", 0.0)

    estimated_stcg = 0.0
    loss_offset = 0.0

    if net_pre_tax > 0:
        if stcl_pool > 0:
            loss_offset = min(stcl_pool, net_pre_tax)
            stcl_pool -= loss_offset
            taxable = net_pre_tax - loss_offset
        else:
            taxable = net_pre_tax
        estimated_stcg = taxable * 0.20
    else:
        stcl_pool += abs(net_pre_tax)

    memory["stcl_pool"] = round(stcl_pool, 2)
    save_json(MEMORY_FILE, memory)

    return {
        "gross_pnl": round(gross_pnl, 2),
        "charges": round(total_charges, 2),
        "loss_offset": round(loss_offset, 2),
        "stcg_tax": round(estimated_stcg, 2),
        "net_in_pocket": round(net_pre_tax - estimated_stcg, 2),
        "shield_remaining": round(stcl_pool, 2),
    }


# =====================================================================
# 7. RELATIVE STRENGTH RESEARCH & SCANNING
# =====================================================================
def get_top_relative_strength_etfs(market_data: dict) -> list[str]:
    if "NIFTYBEES" not in market_data or len(market_data["NIFTYBEES"]) < 20:
        return list(WATCHLIST.keys())

    nifty_df = market_data["NIFTYBEES"]
    nifty_roc = (
        nifty_df["close"].iloc[-1] - nifty_df["close"].iloc[-20]
    ) / nifty_df["close"].iloc[-20]

    rs_scores = {}
    for sym, df in market_data.items():
        if len(df) < 20 or sym == "NIFTYBEES":
            continue
        sector_roc = (
            df["close"].iloc[-1] - df["close"].iloc[-20]
        ) / df["close"].iloc[-20]
        rs_scores[sym] = sector_roc - nifty_roc

    sorted_sectors = sorted(rs_scores, key=rs_scores.get, reverse=True)
    return ["NIFTYBEES"] + sorted_sectors[:2]


def scan_for_buy_entries(market_data: dict, current_portfolio_equity: float):
    now = datetime.now()
    if not (time(9, 30) <= now.time() <= time(14, 45)):
        return

    trades = load_json(DB_FILE, [])
    memory = load_json(MEMORY_FILE, {"cooldowns": {}, "stcl_pool": 0.0})
    active_trades = [t for t in trades if t.get("status") == "OPEN"]

    if len(active_trades) >= MAX_ACTIVE_SLOTS:
        return

    top_picks = get_top_relative_strength_etfs(market_data)
    active_symbols = [t["symbol"] for t in active_trades]
    active_clusters = [
        WATCHLIST[t["symbol"]]["cluster"]
        for t in active_trades
        if t["symbol"] in WATCHLIST
    ]

    slot_capital = round(
        (current_portfolio_equity * 0.90) / MAX_ACTIVE_SLOTS, 2
    )

    for symbol in top_picks:
        if symbol in active_symbols or symbol not in market_data:
            continue

        if symbol in memory.get("cooldowns", {}):
            if now.strftime("%Y-%m-%d") < memory["cooldowns"][symbol]:
                continue

        cluster = WATCHLIST[symbol]["cluster"]
        if cluster == "BROAD_MARKET" and "BROAD_MARKET" in active_clusters:
            continue

        df = market_data[symbol]
        df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()

        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        df["rsi"] = 100 - (100 / (1 + rs))

        latest = df.iloc[-1]

        if latest["close"] > latest["ema20"] and (52 <= latest["rsi"] <= 66):
            entry_price = round(latest["close"], 2)
            sl_price = round(entry_price * (1 - STOP_LOSS_PCT), 2)
            leg1_target = round(entry_price * (1 + BASE_TARGET_PCT), 2)

            risk_per_unit = entry_price - sl_price
            max_rupee_risk = current_portfolio_equity * MAX_RISK_PER_TRADE_PCT

            qty_by_risk = int(max_rupee_risk / risk_per_unit)
            qty_by_cap = int(slot_capital / entry_price)
            units = min(qty_by_risk, qty_by_cap)

            if units >= 2:
                msg = (
                    f"🔔 *BUY RECOMMENDATION: {symbol}*\n"
                    f"*Action:* BUY @ ₹{entry_price} | *SL:* ₹{sl_price} (-2.5%)\n"
                    f"*Target Leg 1 (50%):* ₹{leg1_target} (+3.5%) | *Leg 2:* 10-EMA Trail\n"
                    f"*Sizing:* {units} units (~₹{round(units * entry_price, 2):,.2f}) | *Max Risk:* ₹{round(units * risk_per_unit, 2):,.2f}\n"
                    f"*Slot Status:* Cluster `{cluster}` ({len(active_trades)+1}/{MAX_ACTIVE_SLOTS})\n\n"
                    f"*Justification:*\n"
                    f"• Sector Relative Strength leader trading above 20 EMA with RSI at {latest['rsi']:.1f}.\n"
                    f"• High-probability swing setup with two-tier profit extraction model."
                )
                callback_payload = (
                    f"BUY:{symbol}:{entry_price}:{units}:{sl_price}"
                )
                reply_markup = {
                    "inline_keyboard": [
                        [
                            {
                                "text": "✅ Approve (Take Trade)",
                                "callback_data": callback_payload,
                            },
                            {
                                "text": "❌ Pass",
                                "callback_data": f"PASS:{symbol}",
                            },
                        ]
                    ]
                }
                send_telegram(msg, reply_markup=reply_markup)
                return


# =====================================================================
# 8. EXIT MANAGEMENT ENGINE
# =====================================================================
def evaluate_positions_and_exits(market_data: dict):
    now = datetime.now()
    current_time = now.time()
    trades = load_json(DB_FILE, [])
    updated_trades = []

    for trade in trades:
        if trade.get("status") != "OPEN":
            updated_trades.append(trade)
            continue

        symbol = trade["symbol"]
        if symbol not in market_data:
            updated_trades.append(trade)
            continue

        df = market_data[symbol]
        current_price = df["close"].iloc[-1]
        entry_price = trade["entry_price"]
        units = trade["units"]
        sl = trade["sl"]
        entry_date = datetime.strptime(trade["entry_date"], "%Y-%m-%d")
        days_held = (now - entry_date).days
        pnl_pct = ((current_price - entry_price) / entry_price) * 100

        df["ema10"] = df["close"].ewm(span=10, adjust=False).mean()
        ema10_val = df["ema10"].iloc[-1]

        exit_type = None
        sell_units = 0

        # 1. Opening Gap-Up Harvest (09:15 - 09:30 AM)
        if (
            time(9, 15) <= current_time <= time(9, 30)
            and pnl_pct >= 4.0
            and not trade.get("leg1_done")
        ):
            exit_type = "Opening Gap-Up Profit Harvest"
            sell_units = units // 2
            trade["leg1_done"] = True

        # 2. Standard Leg 1 Target Hit (+3.5%)
        elif (
            current_price >= round(entry_price * (1 + BASE_TARGET_PCT), 2)
            and not trade.get("leg1_done")
        ):
            exit_type = "Leg 1 Target Achieved (+3.5%)"
            sell_units = units // 2
            trade["leg1_done"] = True
            trade["sl"] = entry_price

        # 3. Leg 2 Runner Exit
        elif trade.get("leg1_done"):
            if current_price < ema10_val or current_price <= trade["sl"]:
                exit_type = f"Leg 2 Runner Exit (P&L: {pnl_pct:+.2f}%)"
                sell_units = trade.get("remaining_units", units - (units // 2))
                trade["status"] = "CLOSED"

        # 4. Stop-Loss Hit (-2.5%)
        elif current_price <= sl:
            exit_type = "Stop-Loss Hit (-2.5%)"
            sell_units = units
            trade["status"] = "CLOSED"

        # 5. 30-Day Timeout
        elif days_held >= 30:
            exit_type = f"30-Day Expiry Exit (P&L: {pnl_pct:+.2f}%)"
            sell_units = trade.get("remaining_units", units)
            trade["status"] = "CLOSED"

        if exit_type and sell_units > 0:
            pnl_report = compute_net_pnl(entry_price, current_price, sell_units)

            if trade["status"] == "CLOSED":
                trade["exit_date"] = now.strftime("%Y-%m-%d")
                trade["exit_price"] = current_price
                trade["exit_reason"] = exit_type

            trade["remaining_units"] = (
                trade.get("remaining_units", units) - sell_units
            )

            msg = (
                f"🚨 *EXIT EXECUTED: {symbol}*\n"
                f"*Type:* {exit_type}\n"
                f"*Buy:* ₹{entry_price} ➔ *Sell:* ₹{current_price} | *Units Sold:* {sell_units}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• *Gross P&L:* ₹{pnl_report['gross_pnl']:+,.2f}\n"
                f"• *Charges & Levies:* ₹{pnl_report['charges']:.2f}\n"
                f"• *Loss Offset Applied:* ₹{pnl_report['loss_offset']:.2f}\n"
                f"• *Est. STCG Tax (20%):* ₹{pnl_report['stcg_tax']:.2f}\n"
                f"• *Net In-Pocket Return:* *₹{pnl_report['net_in_pocket']:+,.2f}*\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )
            send_telegram(msg)

        updated_trades.append(trade)

    save_json(DB_FILE, updated_trades)


# =====================================================================
# 9. REFLECTION AGENT
# =====================================================================
def run_post_market_reflection():
    trades = load_json(DB_FILE, [])
    memory = load_json(MEMORY_FILE, {"cooldowns": {}, "stcl_pool": 0.0})
    closed = [t for t in trades if t.get("status") == "CLOSED"]

    for symbol in WATCHLIST.keys():
        sym_closed = [
            t
            for t in closed
            if t.get("symbol") == symbol
            and "Stop-Loss" in t.get("exit_reason", "")
        ]
        sym_closed.sort(key=lambda x: x.get("exit_date", ""), reverse=True)

        if len(sym_closed) >= 2:
            last_date = datetime.strptime(
                sym_closed[0]["exit_date"], "%Y-%m-%d"
            )
            if (datetime.now() - last_date).days <= 7:
                cooldown_until = (
                    datetime.now() + timedelta(days=5)
                ).strftime("%Y-%m-%d")
                memory["cooldowns"][symbol] = cooldown_until
                send_telegram(
                    f"🧠 *REFLECTION AGENT LOG: {symbol}*\n"
                    f"• Placed on 5-day quarantine until {cooldown_until} following repeat stop-outs."
                )

    save_json(MEMORY_FILE, memory)


# =====================================================================
# 10. MAIN EXECUTION PIPELINE
# =====================================================================
if __name__ == "__main__":
    is_open, msg = is_market_open()
    print(f"System State: {msg}")

    # 1. Process incoming Telegram commands (/status, button clicks)
    process_telegram_updates()

    # 2. Fetch market data
    kite = get_kite_session()
    market_data = {}
    to_date = datetime.now()
    from_date = to_date - timedelta(days=60)

    for sym, meta in WATCHLIST.items():
        if kite:
            try:
                records = kite.historical_data(
                    meta["token"], from_date, to_date, "15minute"
                )
                df = pd.DataFrame(records)
                market_data[sym] = df
            except Exception as e:
                print(f"Error fetching data for {sym}: {e}")
        else:
            market_data[sym] = pd.DataFrame(
                {
                    "close": [250.0 + i * 0.5 for i in range(50)],
                    "volume": [10000] * 50,
                }
            )

    # 3. Evaluate active positions and scan for new entries
    evaluate_positions_and_exits(market_data)
    scan_for_buy_entries(market_data, INITIAL_CAPITAL)

    # 4. Generate visual HTML dashboard
    generate_web_dashboard()

    # 5. Post-market reflection after 03:30 PM
    if datetime.now().time() >= time(15, 30):
        run_post_market_reflection()
