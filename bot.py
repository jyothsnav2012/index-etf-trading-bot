import os
import json
import requests
import pyotp
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, time, timedelta
from kiteconnect import KiteConnect

# =====================================================================
# 1. CONSTANTS, RISK RULES & WATCHLIST (10-SECTION ARCHITECTURE)
# =====================================================================
INITIAL_CAPITAL = 100000.0
MAX_ACTIVE_SLOTS = 3
MAX_SLOTS = 3
MAX_RISK_PER_TRADE_PCT = 0.01 # 1% Account Risk per trade (₹1,000)
BASE_TARGET_PCT = 0.035 # +3.5% Target (1:1.4 R:R)
STOP_LOSS_PCT = 0.025 # -2.5% Stop-Loss
MAX_DRAWDOWN_CIRCUIT_PCT = 0.08 # 8% Drawdown Circuit Breaker
STCL_SET_ASIDE_PCT = 0.20 # 20% of STCL credited back to reinvestment
MAX_LEG2_TIME_STOP_DAYS = 15 # Leg 2 structural exit threshold

DB_FILE = "trade_database.json"
MEMORY_FILE = "strategy_memory.json"
HOLIDAYS_CACHE_FILE = "holidays_cache.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# YFinance Tickers for NSE Instruments
WATCHLIST = {
    # Core Indices & Market Cap
    "NIFTYBEES": {"ticker": "NIFTYBEES.NS", "cluster": "BROAD_MARKET"},
    "JUNIORBEES": {"ticker": "JUNIORBEES.NS", "cluster": "BROAD_MARKET"},
    "MID150BEES": {"ticker": "MID150BEES.NS", "cluster": "BROAD_MARKET"},
    "BANKBEES": {"ticker": "BANKBEES.NS", "cluster": "BROAD_MARKET"},
    
    # Sector & Thematic
    "ITBEES": {"ticker": "ITBEES.NS", "cluster": "TECH_EXPORT"},
    "AUTOBEES": {"ticker": "AUTOBEES.NS", "cluster": "AUTO_CYCLICAL"},
    "PHARMABEES": {"ticker": "PHARMABEES.NS", "cluster": "HEALTHCARE_DEFENSIVE"},
    "FMCGIETF": {"ticker": "FMCGIETF.NS", "cluster": "CONSUMER_DEFENSIVE"},
    
    # Global & Commodities
    "MON100": {"ticker": "MON100.NS", "cluster": "GLOBAL_TECH"},
    "GOLDBEES": {"ticker": "GOLDBEES.NS", "cluster": "COMMODITY_HEDGE"},
    "SILVERBEES": {"ticker": "SILVERBEES.NS", "cluster": "COMMODITY_HEDGE"}
}

# =====================================================================
# 2. STATE STORAGE & CACHE HELPERS
# =====================================================================
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
# 3. TELEGRAM DISPATCH & INTERACTIVE CALLBACK LISTENER
# =====================================================================
def send_telegram(message: str, reply_markup: dict = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Telegram Error: Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secrets.")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": str(TELEGRAM_CHAT_ID).strip(),
        "text": message
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
        
    try:
        res = requests.post(url, json=payload, timeout=10).json()
        if not res.get("ok"):
            print(f"❌ Telegram Send Failed: {res.get('description')}")
        else:
            print("✅ Telegram notification sent successfully.")
    except Exception as e:
        print(f"Telegram Dispatch Exception: {e}")


def process_telegram_updates():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        res = requests.get(url, timeout=10).json()
        updates = res.get("result", [])
        print(f"Telegram Polling: Found {len(updates)} pending updates.")
        if not updates:
            return
            
        trades = load_json(DB_FILE, [])
        memory = load_json(MEMORY_FILE, {"stcl_pool": 0.0, "cooldowns": {}, "portfolio_peak": INITIAL_CAPITAL})
        active_trades = [t for t in trades if t.get("status") == "OPEN"]
        active_symbols = [t["symbol"] for t in active_trades]
        
        last_update_id = None

        for item in updates:
            last_update_id = item["update_id"]
            
            # --- 1. Handle Inline Button Callback (BUY / PASS clicks) ---
            if "callback_query" in item:
                cb = item["callback_query"]
                cb_id = cb.get("id")
                cb_data = cb.get("data", "")
                
                # Acknowledge callback immediately to remove loading spinner in Telegram
                try:
                    ack_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
                    requests.post(ack_url, json={"callback_query_id": cb_id}, timeout=5)
                except Exception:
                    pass
                
                if cb_data.startswith("BUY:"):
                    parts = cb_data.split(":")
                    if len(parts) >= 5:
                        _, sym, entry_str, qty_str, sl_str = parts[:5]
                        entry_price = float(entry_str)
                        qty = int(qty_str)
                        sl_price = float(sl_str)
                        
                        # Check available slots (Max 3)
                        current_open = [t for t in trades if t.get("status") == "OPEN"]
                        if len(current_open) >= MAX_SLOTS:
                            send_telegram(f"⚠️ Cannot execute BUY for {sym}: Maximum {MAX_SLOTS} slots already filled.")
                        elif sym in [t["symbol"] for t in current_open]:
                            send_telegram(f"ℹ️ Position for {sym} is already open.")
                        else:
                            new_trade = {
                                "symbol": sym,
                                "entry_price": entry_price,
                                "units": qty,
                                "remaining_units": qty,
                                "sl": sl_price,
                                "entry_date": datetime.now(IST).strftime("%Y-%m-%d"),
                                "status": "OPEN",
                                "leg1_done": False,
                                "exit_price": None,
                                "exit_date": None,
                                "exit_reason": None
                            }
                            trades.append(new_trade)
                            save_json(DB_FILE, trades)
                            send_telegram(f"✅ Paper Trade Confirmed: Bought {qty} units of {sym} @ ₹{entry_price:.2f} (SL: ₹{sl_price:.2f}).")
                
                elif cb_data.startswith("PASS:"):
                    sym = cb_data.split(":")[1] if ":" in cb_data else "Signal"
                    send_telegram(f"⏭️ Signal for {sym} passed.")

            # --- 2. Handle Text Commands (/status, /dashboard, /buy <symbol>) ---
            elif "message" in item:
                msg = item["message"]
                text = msg.get("text", "").strip()
                
                if text in ["/start", "/status", "/dashboard", "/pnl"]:
                    pos_text = ""
                    current_open = [t for t in trades if t.get("status") == "OPEN"]
                    if not current_open:
                        pos_text = "• Active Slots: 0/3 (100% Cash)\n"
                    else:
                        for t in current_open:
                            pos_text += f"• `{t['symbol']}`: {t['units']} units @ ₹{t['entry_price']} (SL: ₹{t['sl']})\n"
                            
                    status_report = (
                        "📊 SWING ENGINE STATUS\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "🟢 Status: Online & Active\n"
                        f"⏰ Server Time: {datetime.now(IST).strftime('%H:%M:%S IST')}\n"
                        f"💰 Capital Base: ₹{INITIAL_CAPITAL:,.2f}\n"
                        f"🛡️ Tax Shield: ₹{memory.get('stcl_pool', 0.0):,.2f}\n\n"
                        f"Open Positions ({len(current_open)}/{MAX_SLOTS}):\n"
                        f"{pos_text}"
                        "━━━━━━━━━━━━━━━━━━━━"
                    )
                    send_telegram(status_report)
                    
                elif text.startswith("/buy "):
                    sym = text.split()[1].upper().replace(".NS", "")
                    current_open = [t for t in trades if t.get("status") == "OPEN"]
                    if len(current_open) >= MAX_SLOTS:
                        send_telegram(f"⚠️ Cannot execute BUY for {sym}: Maximum {MAX_SLOTS} slots already filled.")
                    elif sym in [t["symbol"] for t in current_open]:
                        send_telegram(f"ℹ️ Position for {sym} is already active.")
                    else:
                        # Auto-fetch latest close if bought manually via text
                        try:
                            ticker_key = f"{sym}.NS" if not sym.endswith(".NS") else sym
                            df = yf.download(ticker_key, period="5d", interval="1d", progress=False)
                            if not df.empty:
                                close_p = float(df['Close'].iloc[-1])
                                slot_cap = INITIAL_CAPITAL / MAX_SLOTS
                                units = int(slot_cap / close_p)
                                sl = round(close_p * (1.0 - STOP_LOSS_PCT), 2)
                                new_trade = {
                                    "symbol": sym,
                                    "entry_price": round(close_p, 2),
                                    "units": units,
                                    "remaining_units": units,
                                    "sl": sl,
                                    "entry_date": datetime.now(IST).strftime("%Y-%m-%d"),
                                    "status": "OPEN",
                                    "leg1_done": False,
                                    "exit_price": None,
                                    "exit_date": None,
                                    "exit_reason": None
                                }
                                trades.append(new_trade)
                                save_json(DB_FILE, trades)
                                send_telegram(f"✅ Paper Trade Confirmed: Bought {units} units of {sym} @ ₹{close_p:.2f} (SL: ₹{sl:.2f}).")
                        except Exception as ex:
                            send_telegram(f"❌ Error executing text order for {sym}: {ex}")

        # Clear processed updates from Telegram's queue
        if last_update_id is not None:
            try:
                requests.get(f"{url}?offset={last_update_id + 1}", timeout=5)
            except Exception:
                pass

    except Exception as e:
        print(f"Telegram polling error: {e}")


# =====================================================================
# 4. STATIC WEB DASHBOARD GENERATOR (docs/index.html)
# =====================================================================
def generate_web_dashboard():
    os.makedirs("docs", exist_ok=True)
    trades = load_json(DB_FILE, [])
    memory = load_json(MEMORY_FILE, {"stcl_pool": 0.0, "cooldowns": {}, "portfolio_peak": INITIAL_CAPITAL})
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
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 16px; }}
        .container {{ max-width: 650px; margin: 0 auto; }}
        h2 {{ font-size: 20px; margin-bottom: 16px; color: #38bdf8; display: flex; justify-content: space-between; align-items: center; }}
        .badge {{ background: #10b981; color: white; padding: 4px 8px; border-radius: 6px; font-size: 11px; }}
        .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }}
        .card {{ background: #1e293b; border-radius: 12px; padding: 14px; border: 1px solid #334155; }}
        .card-label {{ font-size: 12px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; }}
        .card-val {{ font-size: 18px; font-weight: bold; margin-top: 6px; }}
        .section-card {{ background: #1e293b; border-radius: 12px; padding: 16px; margin-bottom: 16px; border: 1px solid #334155; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        th, td {{ text-align: left; padding: 10px 4px; border-bottom: 1px solid #334155; }}
        th {{ color: #94a3b8; font-size: 11px; text-transform: uppercase; }}
    </style>
</head>
<body>
    <div class="container">
        <h2><span>⚡ Multi-Agent ETF Engine</span><span class="badge">ONLINE</span></h2>
        <div class="grid">
            <div class="card"><div class="card-label">Last Polled</div><div class="card-val">{datetime.now().strftime('%H:%M IST')}</div></div>
            <div class="card"><div class="card-label">Active Slots</div><div class="card-val">{len(active)} / {MAX_ACTIVE_SLOTS}</div></div>
            <div class="card"><div class="card-label">Capital Base</div><div class="card-val">₹1,00,000</div></div>
            <div class="card"><div class="card-label">Tax Shield</div><div class="card-val">₹{memory.get('stcl_pool', 0.0):,.2f}</div></div>
        </div>
        <div class="section-card">
            <div style="font-weight:bold; margin-bottom:10px; color:#e2e8f0;">Active Positions</div>
            <table><thead><tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Stop-Loss</th></tr></thead><tbody>{active_rows}</tbody></table>
        </div>
        <div class="section-card">
            <div style="font-weight:bold; margin-bottom:10px; color:#e2e8f0;">Recent Trades</div>
            <table><thead><tr><th>Symbol</th><th>Exit Reason</th><th>Date</th></tr></thead><tbody>{closed_rows}</tbody></table>
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
            timeout=10
        ).json()

        if login_res.get("status") != "success":
            print(f"❌ Step 1 (Login) Failed: {login_res.get('message', 'Check User ID / Password')}")
            return None
        print("✅ Step 1: User ID and Password verified.")

        request_id = login_res["data"]["request_id"]

        # Step 2: TOTP 2FA
        twofa_res = session.post(
            "https://kite.zerodha.com/api/twofa",
            data={"user_id": user_id.strip(), "request_id": request_id, "twofa_value": totp, "skip_session": ""},
            timeout=10
        ).json()

        if twofa_res.get("status") != "success":
            print(f"❌ Step 2 (2FA) Failed: {twofa_res.get('message', 'Check KITE_TOTP_SECRET key')}")
            return None
        print("✅ Step 2: 2FA TOTP verified.")

        # Step 3: OAuth Token Extraction
        req_url = f"https://kite.zerodha.com/connect/login?api_key={api_key.strip()}&v=3"
        request_token = None

        try:
            resp = session.get(req_url, allow_redirects=True, timeout=10)
            if "request_token=" in resp.url:
                request_token = resp.url.split("request_token=")[1].split("&")[0]
        except requests.exceptions.ConnectionError as ce:
            url_str = str(ce)
            if "request_token=" in url_str:
                request_token = url_str.split("request_token=")[1].split("&")[0].split(" ")[0].rstrip("')\"")

        if not request_token:
            print("❌ Step 3 Failed: Could not parse request_token.")
            return None

        data = kite.generate_session(request_token, api_secret=api_secret.strip())
        kite.set_access_token(data["access_token"])
        print("✅ Step 3: Session established. Connected to Kite live feed.")
        return kite

    except Exception as e:
        print(f"❌ Kite Connect Exception: {e}")
        return None

# =====================================================================
# 6. MARKET TIMING & TAX ENGINE
# =====================================================================
def get_trading_holidays() -> set:
    cache = load_json(HOLIDAYS_CACHE_FILE, None)
    current_year = datetime.now().year

    if cache and cache.get("year") == current_year:
        return set(cache.get("holidays", []))

    holidays = {
        f"{current_year}-01-26", f"{current_year}-03-14", f"{current_year}-03-31",
        f"{current_year}-04-10", f"{current_year}-04-14", f"{current_year}-05-01",
        f"{current_year}-08-15", f"{current_year}-10-02", f"{current_year}-10-21",
        f"{current_year}-11-10", f"{current_year}-12-25"
    }
    save_json(HOLIDAYS_CACHE_FILE, {"year": current_year, "holidays": list(holidays)})
    return holidays

def is_market_open():
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5:
        return False, "Market is closed (Weekend)."
    if today_str in get_trading_holidays():
        return False, "Market is closed (NSE Holiday)."
    if not (time(9, 15) <= now.time() <= time(15, 30)):
        return False, f"Outside trading hours ({now.strftime('%H:%M IST')})."
    return True, "Market Active"

# =====================================================================
# 7. AGENT 1 & 2: DATA SENTINEL & REGIME SENTINEL (VIA YFINANCE)
# =====================================================================
def fetch_indicators_and_regime():
    market_data = {}

    for sym, meta in WATCHLIST.items():
        try:
            ticker_obj = yf.Ticker(meta["ticker"])
            df = ticker_obj.history(period="6mo", interval="1d")
            
            if not df.empty and len(df) >= 30:
                df.columns = [c.lower() for c in df.columns]
                df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
                df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
                
                # RSI 14
                delta = df["close"].diff()
                gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                rs = gain / loss
                df["rsi"] = 100 - (100 / (1 + rs))
                
                # ATR 14
                tr1 = df["high"] - df["low"]
                tr2 = (df["high"] - df["close"].shift()).abs()
                tr3 = (df["low"] - df["close"].shift()).abs()
                df["atr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1).rolling(14).mean()
                
                market_data[sym] = df
        except Exception as e:
            print(f"Error fetching data for {sym}: {e}")

    regime = "NORMAL"
    nifty_df = market_data.get("NIFTYBEES")
    if nifty_df is not None and not nifty_df.empty:
        if nifty_df.iloc[-1]["close"] < nifty_df.iloc[-1]["ema50"]:
            regime = "DEFENSIVE"
    
    print(f"Regime Sentinel: Market regime evaluated as '{regime}'")
    return market_data, regime

# =====================================================================
# 8. AGENT 3 & 4: ALPHA ENGINE & RISK MANAGER
# =====================================================================
def manage_positions_and_scan(market_data, regime):
    trades = load_json(DB_FILE, [])
    memory = load_json(MEMORY_FILE, {"stcl_pool": 0.0, "cooldowns": {}, "portfolio_peak": INITIAL_CAPITAL})
    
    active_trades = [t for t in trades if t.get("status") == "OPEN"]
    active_clusters = [WATCHLIST[t["symbol"]]["cluster"] for t in active_trades if t["symbol"] in WATCHLIST]

    # --- Position Management (Exits) ---
    for t in active_trades:
        sym = t["symbol"]
        df = market_data.get(sym)
        if df is None or df.empty:
            continue
        
        current_price = df.iloc[-1]["close"]
        entry_price = t["entry_price"]
        units = t["units"]
        sl = t["sl"]
        target = entry_price * (1 + BASE_TARGET_PCT)

        # Leg 1 Profit Booking (+3.5%)
        if not t.get("leg1_done", False) and current_price >= target:
            half_qty = units // 2
            t["remaining_units"] = units - half_qty
            t["leg1_done"] = True
            t["sl"] = entry_price
            save_json(DB_FILE, trades)
            send_telegram(
                f"🎯 *LEG 1 PROFIT BOOKED: {sym}*\n"
                f"• Booked: {half_qty} units @ ₹{current_price:.2f} (+3.5%)\n"
                f"• Trailing SL updated to cost: ₹{entry_price:.2f}"
            )

        # Stop-Loss Hit
        elif current_price <= sl:
            t["status"] = "CLOSED"
            t["exit_price"] = current_price
            t["exit_date"] = datetime.now().strftime("%Y-%m-%d")
            t["exit_reason"] = "Stop-Loss Hit"
            loss_amount = (entry_price - current_price) * t.get("remaining_units", units)
            memory["stcl_pool"] += loss_amount * STCL_SET_ASIDE_PCT
            save_json(DB_FILE, trades)
            save_json(MEMORY_FILE, memory)
            send_telegram(
                f"🛑 *STOP-LOSS HIT: {sym}*\n"
                f"• Exited {t.get('remaining_units', units)} units @ ₹{current_price:.2f}\n"
                f"• Loss: ₹{loss_amount:.2f} | STCL Shield: ₹{memory['stcl_pool']:.2f}"
            )

    # --- Opportunity Scanner (Alpha Signals) ---
    if len(active_trades) < MAX_ACTIVE_SLOTS:
        available_slots = MAX_ACTIVE_SLOTS - len(active_trades)
        for sym, df in market_data.items():
            if available_slots <= 0:
                break
            if sym in [t["symbol"] for t in active_trades]:
                continue
            
            cluster = WATCHLIST[sym]["cluster"]
            if cluster in active_clusters:
                continue

            last_row = df.iloc[-1]
            close = last_row["close"]
            ema20 = last_row["ema20"]
            ema50 = last_row["ema50"]
            rsi = last_row["rsi"]

            if (close > ema50) and (abs(close - ema20) / close <= 0.015) and (40 <= rsi <= 60):
                slot_capital = (INITIAL_CAPITAL / MAX_ACTIVE_SLOTS) + (memory.get("stcl_pool", 0.0) / MAX_ACTIVE_SLOTS)
                qty = int(slot_capital // close)
                stop_loss = round(close * (1 - STOP_LOSS_PCT), 2)

                if qty > 0:
                    inline_keyboard = {
                        "inline_keyboard": [
                            [
                                {"text": f"✅ Buy {qty} units", "callback_data": f"BUY:{sym}:{round(close,2)}:{qty}:{stop_loss}"},
                                {"text": "❌ Pass", "callback_data": f"PASS:{sym}"}
                            ]
                        ]
                    }
                    signal_card = (
                        f"⚡ *NEW SWING SIGNAL DETECTED*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📈 *Symbol:* `{sym}` ({cluster})\n"
                        f"💰 *Entry:* ₹{close:.2f}\n"
                        f"🛑 *Stop Loss:* ₹{stop_loss} (-2.5%)\n"
                        f"🎯 *Target 1:* ₹{round(close * (1 + BASE_TARGET_PCT), 2)} (+3.5%)\n"
                        f"📦 *Position Size:* {qty} units (~₹{qty * close:,.2f})\n"
                        f"📊 *RSI 14:* {rsi:.1f}\n"
                        f"━━━━━━━━━━━━━━━━━━━━"
                    )
                    send_telegram(signal_card, reply_markup=inline_keyboard)
                    available_slots -= 1

# =====================================================================
# 9. AGENT 5 & 6: TAX SHIELD & EXECUTION AGENT
# =====================================================================
def run_trading_engine():
    market_data, regime = fetch_indicators_and_regime()
    manage_positions_and_scan(market_data, regime)

# =====================================================================
# 10. MAIN CONTROLLER
# =====================================================================
if __name__ == "__main__":
    is_open, msg = is_market_open()
    print(f"Status: {msg}")

    process_telegram_updates()
    kite = get_kite_session()

    run_trading_engine()
    generate_web_dashboard()

    # --- ADD THIS: Proactive heartbeat to Telegram on every run ---
    trades = load_json(DB_FILE, [])
    memory = load_json(
        MEMORY_FILE,
        {
            "stcl_pool": 0.0,
            "cooldowns": {},
            "portfolio_peak": INITIAL_CAPITAL,
        },
    )
    active_trades = [t for t in trades if t.get("status") == "OPEN"]

    pos_text = ""
    if not active_trades:
        pos_text = "• *Active Slots:* 0/3 (100% Cash)\n"
    else:
        for t in active_trades:
            pos_text += f"• `{t['symbol']}`: {t['units']} units @ ₹{t['entry_price']} (SL: ₹{t['sl']})\n"

    periodic_status = (
        f"📊 *SWING ENGINE STATUS*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 *Status:* Scan Complete\n"
        f"⏰ *Server Time:* {datetime.now().strftime('%H:%M:%S IST')}\n"
        f"💰 *Capital Base:* ₹1,00,000.00\n"
        f"🛡️ *Tax Shield:* ₹{memory.get('stcl_pool', 0.0):,.2f}\n\n"
        f"*Open Positions ({len(active_trades)}/{MAX_ACTIVE_SLOTS}):*\n{pos_text}"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    send_telegram(periodic_status)
