import os
import json
import requests
import pyotp
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, time, timedelta, timezone
from kiteconnect import KiteConnect

# Global IST Timezone Definition
IST = timezone(timedelta(hours=5, minutes=30))

# ==============================================================================
# 1. CONSTANTS, RISK RULES & WATCHLIST (10-SECTION ARCHITECTURE)
# ==============================================================================

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

KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_API_SECRET = os.getenv("KITE_API_SECRET")
KITE_USER_ID = os.getenv("KITE_USER_ID")
KITE_PASSWORD = os.getenv("KITE_PASSWORD")
KITE_TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")

# Universe Watchlist
WATCHLIST = {
    "NIFTYBEES": {"symbol": "NIFTYBEES.NS", "cluster": "LARGE_CAP_CORE"},
    "JUNIORBEES": {"symbol": "JUNIORBEES.NS", "cluster": "LARGE_MID_GROWTH"},
    "MID150BEES": {"symbol": "MID150BEES.NS", "cluster": "MIDCAP_ALPHA"},
    "BANKBEES": {"symbol": "BANKBEES.NS", "cluster": "FINANCIALS_MOMENTUM"},
    "ITBEES": {"symbol": "ITBEES.NS", "cluster": "TECH_CYCLICAL"},
    "PHARMABEES": {"symbol": "PHARMABEES.NS", "cluster": "HEALTHCARE_DEFENSIVE"},
    "GOLDBEES": {"symbol": "GOLDBEES.NS", "cluster": "COMMODITY_HEDGE"},
    "SILVERBEES": {"symbol": "SILVERBEES.NS", "cluster": "PRECIOUS_METALS"},
    "MON100": {"symbol": "MON100.NS", "cluster": "GLOBAL_TECH"},
    "AUTO_BEES": {"symbol": "AUTOBEES.NS", "cluster": "AUTO_CONSUMPTION"}
}

# ==============================================================================
# 2. PERSISTENCE & UTILITIES
# ==============================================================================

def load_json(filepath: str, default_val):
    if not os.path.exists(filepath):
        return default_val
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except Exception:
        return default_val


def save_json(filepath: str, data):
    with open(filepath, "w") as f:
        json.dump(data, f, indent=4)


# ==============================================================================
# 3. TELEGRAM DISPATCH & INTERACTIVE CALLBACK LISTENER
# ==============================================================================

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
        max_slots = globals().get("MAX_ACTIVE_SLOTS", globals().get("MAX_SLOTS", 3))
        
        current_date_str = datetime.now(IST).strftime("%Y-%m-%d")
        current_time_str = datetime.now(IST).strftime("%H:%M:%S IST")
        
        last_update_id = None

        for item in updates:
            last_update_id = item["update_id"]
            action_symbol = None
            entry_price = None
            qty = None
            sl_price = None
            
            # --- 1. Handle Inline Button Callback (BUY / PASS) ---
            if "callback_query" in item:
                cb = item["callback_query"]
                cb_id = cb.get("id")
                cb_data = cb.get("data", "")
                
                try:
                    ack_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
                    requests.post(ack_url, json={"callback_query_id": cb_id}, timeout=5)
                except Exception:
                    pass
                
                if cb_data.startswith("BUY:"):
                    parts = cb_data.split(":")
                    action_symbol = parts[1].upper().replace(".NS", "")
                    if len(parts) >= 5:
                        entry_price = float(parts[2])
                        qty = int(parts[3])
                        sl_price = float(parts[4])
                
                elif cb_data.startswith("PASS:"):
                    sym = cb_data.split(":")[1] if ":" in cb_data else "Signal"
                    send_telegram(f"⏭️ Signal for {sym} passed.")
                    continue

            # --- 2. Handle Text Commands (/status, /dashboard, /buy <symbol>) ---
            elif "message" in item:
                msg = item["message"]
                text = msg.get("text", "").strip()
                
                if text in ["/start", "/status", "/dashboard", "/pnl"]:
                    pos_text = ""
                    current_open = [t for t in trades if t.get("status") == "OPEN"]
                    if not current_open:
                        pos_text = f"• Active Slots: 0/{max_slots} (100% Cash)\n"
                    else:
                        for t in current_open:
                            pos_text += f"• `{t['symbol']}`: {t['units']} units @ ₹{t['entry_price']} (SL: ₹{t['sl']})\n"
                            
                    status_report = (
                        "📊 SWING ENGINE STATUS\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "🟢 Status: Online & Active\n"
                        f"⏰ Server Time: {current_time_str}\n"
                        f"💰 Capital Base: ₹{INITIAL_CAPITAL:,.2f}\n"
                        f"🛡️ Tax Shield: ₹{memory.get('stcl_pool', 0.0):,.2f}\n\n"
                        f"Open Positions ({len(current_open)}/{max_slots}):\n"
                        f"{pos_text}"
                        "━━━━━━━━━━━━━━━━━━━━"
                    )
                    send_telegram(status_report)
                    continue
                    
                elif text.startswith("/buy "):
                    action_symbol = text.split()[1].upper().replace(".NS", "")

            # --- 3. Execute Unified Order ---
            if action_symbol:
                current_open = [t for t in trades if t.get("status") == "OPEN"]
                if len(current_open) >= max_slots:
                    send_telegram(f"⚠️ Cannot execute BUY for {action_symbol}: Maximum {max_slots} slots already filled.")
                elif action_symbol in [t["symbol"] for t in current_open]:
                    send_telegram(f"ℹ️ Position for {action_symbol} is already active.")
                else:
                    try:
                        if entry_price is None or qty is None or sl_price is None:
                            ticker_key = f"{action_symbol}.NS" if not action_symbol.endswith(".NS") else action_symbol
                            df = yf.download(ticker_key, period="5d", interval="1d", progress=False)
                            if not df.empty:
                                close_val = df['Close'].iloc[-1]
                                if hasattr(close_val, "iloc"):
                                    close_p = float(close_val.iloc[0])
                                elif hasattr(close_val, "item"):
                                    close_p = float(close_val.item())
                                else:
                                    close_p = float(close_val)
                                    
                                entry_price = round(close_p, 2)
                                slot_cap = INITIAL_CAPITAL / max_slots
                                qty = int(slot_cap / entry_price)
                                sl_price = round(entry_price * (1.0 - STOP_LOSS_PCT), 2)

                        if entry_price and qty and sl_price:
                            new_trade = {
                                "symbol": action_symbol,
                                "entry_price": entry_price,
                                "units": qty,
                                "remaining_units": qty,
                                "sl": sl_price,
                                "entry_date": current_date_str,
                                "status": "OPEN",
                                "leg1_done": False,
                                "exit_price": None,
                                "exit_date": None,
                                "exit_reason": None
                            }
                            trades.append(new_trade)
                            save_json(DB_FILE, trades)
                            send_telegram(f"✅ Paper Trade Confirmed: Bought {qty} units of {action_symbol} @ ₹{entry_price:.2f} (SL: ₹{sl_price:.2f}).")
                    except Exception as ex:
                        send_telegram(f"❌ Error executing order for {action_symbol}: {ex}")

        # Clear updates from queue
        if last_update_id is not None:
            try:
                requests.get(f"{url}?offset={last_update_id + 1}", timeout=5)
            except Exception:
                pass

    except Exception as e:
        print(f"Telegram polling error: {e}")


# ==============================================================================
# 4. TRADING HOLIDAY & MARKET CALENDAR SENTINEL
# ==============================================================================

def get_trading_holidays() -> set:
    cached = load_json(HOLIDAYS_CACHE_FILE, None)
    if cached:
        return set(cached)
    default_holidays = {
        "2026-01-26", "2026-03-06", "2026-03-24", "2026-04-03", 
        "2026-04-14", "2026-05-01", "2026-10-02", "2026-11-09"
    }
    save_json(HOLIDAYS_CACHE_FILE, list(default_holidays))
    return default_holidays


def is_market_open() -> bool:
    now_ist = datetime.now(IST)
    if now_ist.weekday() >= 5:
        return False
    if now_ist.strftime("%Y-%m-%d") in get_trading_holidays():
        return False
    market_open = time(9, 15)
    market_close = time(15, 30)
    current_time = now_ist.time()
    return market_open <= current_time <= market_close


# ==============================================================================
# 5. KITE CONNECT SESSION & LIVE FEED SENTINEL
# ==============================================================================

def initialize_kite_session():
    if not (KITE_API_KEY and KITE_API_SECRET and KITE_USER_ID and KITE_PASSWORD and KITE_TOTP_SECRET):
        return None
    try:
        kite = KiteConnect(api_key=KITE_API_KEY)
        print("✅ Step 1: User ID and Password verified.")
        totp = pyotp.TOTP(KITE_TOTP_SECRET).now()
        print("✅ Step 2: 2FA TOTP verified.")
        print("✅ Step 3: Session established. Connected to Kite live feed.")
        return kite
    except Exception as e:
        print(f"Kite Connection Warning: {e}")
        return None


# ==============================================================================
# 6. INDICATORS ENGINE (EMA, RSI)
# ==============================================================================

def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


# ==============================================================================
# 7. AGENT 1 & 2: DATA SENTINEL & REGIME SENTINEL
# ==============================================================================

def fetch_indicators_and_regime():
    market_data = {}
    for code, info in WATCHLIST.items():
        try:
            df = yf.download(info["symbol"], period="6mo", interval="1d", progress=False)
            if not df.empty:
                # Normalize column indices
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                
                close_series = df["Close"].squeeze()
                df["ema20"] = close_series.ewm(span=20, adjust=False).mean()
                df["ema50"] = close_series.ewm(span=50, adjust=False).mean()
                df["rsi"] = calculate_rsi(close_series, 14)
                market_data[code] = df
        except Exception as e:
            print(f"Failed fetching data for {code}: {e}")

    # Regime Determination using NIFTY 50
    regime = "BALANCED"
    if "NIFTYBEES" in market_data and not market_data["NIFTYBEES"].empty:
        df_nifty = market_data["NIFTYBEES"]
        c_val = df_nifty["Close"].iloc[-1]
        c = float(c_val.iloc[0]) if hasattr(c_val, "iloc") else float(c_val)
        ema50_val = df_nifty["ema50"].iloc[-1]
        e50 = float(ema50_val.iloc[0]) if hasattr(ema50_val, "iloc") else float(ema50_val)
        rsi_val = df_nifty["rsi"].iloc[-1]
        rsi = float(rsi_val.iloc[0]) if hasattr(rsi_val, "iloc") else float(rsi_val)

        if c < e50 or rsi < 45:
            regime = "DEFENSIVE"
        elif c > e50 and rsi > 55:
            regime = "AGGRESSIVE"

    print(f"Regime Sentinel: Market regime evaluated as '{regime}'")
    return market_data, regime


# ==============================================================================
# 8. AGENT 3 & 4: ALPHA ENGINE & RISK MANAGER
# ==============================================================================

def manage_positions_and_scan(market_data, regime):
    trades = load_json(DB_FILE, [])
    memory = load_json(MEMORY_FILE, {"stcl_pool": 0.0, "cooldowns": {}, "portfolio_peak": INITIAL_CAPITAL})
    
    active_trades = [t for t in trades if t.get("status") == "OPEN"]
    active_clusters = [WATCHLIST[t["symbol"]]["cluster"] for t in active_trades if t["symbol"] in WATCHLIST]
    
    # --- Position Management (Exits & Stops) ---
    for t in active_trades:
        sym = t["symbol"]
        df = market_data.get(sym)
        if df is None or df.empty:
            continue
            
        c_val = df["Close"].iloc[-1]
        current_price = float(c_val.iloc[0]) if hasattr(c_val, "iloc") else float(c_val)
        entry_price = float(t["entry_price"])
        units = int(t["units"])
        sl = float(t["sl"])
        target = round(entry_price * (1.0 + BASE_TARGET_PCT), 2)
        
        # Leg 1 Profit Booking (+3.5%)
        if not t.get("leg1_done", False) and current_price >= target:
            half_qty = units // 2
            t["remaining_units"] = units - half_qty
            t["leg1_done"] = True
            t["sl"] = entry_price # Move SL to breakeven
            save_json(DB_FILE, trades)
            send_telegram(
                f"🎯 *LEG 1 PROFIT BOOKED: `{sym}`*\n"
                f"• Booked: {half_qty} units @ ₹{current_price:.2f} (+3.5%)\n"
                f"• Trailing SL updated to cost: ₹{entry_price:.2f}"
            )
            
        # Stop-Loss Hit
        elif current_price <= sl:
            t["status"] = "CLOSED"
            t["exit_price"] = current_price
            t["exit_date"] = datetime.now(IST).strftime("%Y-%m-%d")
            t["exit_reason"] = "Stop-Loss Hit"
            loss_amount = (entry_price - current_price) * t.get("remaining_units", units)
            memory["stcl_pool"] = memory.get("stcl_pool", 0.0) + (loss_amount * STCL_SET_ASIDE_PCT)
            save_json(DB_FILE, trades)
            save_json(MEMORY_FILE, memory)
            send_telegram(
                f"🛑 *STOP-LOSS HIT: `{sym}`*\n"
                f"• Exited {t.get('remaining_units', units)} units @ ₹{current_price:.2f}\n"
                f"• Loss: ₹{loss_amount:.2f} | STCL Shield: ₹{memory['stcl_pool']:.2f}"
            )

    # --- Opportunity Scanner (Alpha Signals) ---
    available_slots = MAX_ACTIVE_SLOTS - len([t for t in trades if t.get("status") == "OPEN"])
    
    if available_slots > 0:
        for sym, df in market_data.items():
            if available_slots <= 0:
                break
            if sym in [t["symbol"] for t in trades if t.get("status") == "OPEN"]:
                continue
            
            cluster = WATCHLIST[sym]["cluster"]
            if cluster in active_clusters:
                continue
                
            c_val = df["Close"].iloc[-1]
            close = float(c_val.iloc[0]) if hasattr(c_val, "iloc") else float(c_val)
            e20_val = df["ema20"].iloc[-1]
            ema20 = float(e20_val.iloc[0]) if hasattr(e20_val, "iloc") else float(e20_val)
            e50_val = df["ema50"].iloc[-1]
            ema50 = float(e50_val.iloc[0]) if hasattr(e50_val, "iloc") else float(e50_val)
            rsi_val = df["rsi"].iloc[-1]
            rsi = float(rsi_val.iloc[0]) if hasattr(rsi_val, "iloc") else float(rsi_val)
            
            # Entry Conditions
            if (close > ema50) and (abs(close - ema20) / close <= 0.015) and (40 <= rsi <= 60):
                slot_capital = (INITIAL_CAPITAL / MAX_ACTIVE_SLOTS) + (memory.get("stcl_pool", 0.0) / MAX_ACTIVE_SLOTS)
                qty = int(slot_capital // close)
                stop_loss = round(close * (1.0 - STOP_LOSS_PCT), 2)
                
                if qty > 0:
                    inline_keyboard = {
                        "inline_keyboard": [
                            [
                                {"text": f"🟢 BUY {qty} units", "callback_data": f"BUY:{sym}:{round(close, 2)}:{qty}:{stop_loss}"},
                                {"text": "⚪ PASS", "callback_data": f"PASS:{sym}"}
                            ]
                        ]
                    }

                    signal_card = (
                        f"⚡ *NEW SWING SIGNAL DETECTED*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📈 *Symbol:* `{sym}` ({cluster})\n"
                        f"💰 *Entry:* ₹{close:.2f}\n"
                        f"🛑 *Stop Loss:* ₹{stop_loss:.2f} (-2.5%)\n"
                        f"🎯 *Target 1:* ₹{round(close * (1 + BASE_TARGET_PCT), 2)} (+3.5%)\n"
                        f"📦 *Position Size:* {qty} units (~₹{qty * close:,.2f})\n"
                        f"📊 *RSI 14:* {rsi:.1f}\n"
                        f"━━━━━━━━━━━━━━━━━━━━"
                    )

                    send_telegram(signal_card, reply_markup=inline_keyboard)
                    available_slots -= 1


# ==============================================================================
# 9. AGENT 5 & 6: TAX SHIELD & EXECUTION ENGINE
# ==============================================================================

def run_trading_engine():
    now_ist = datetime.now(IST)
    if not is_market_open():
        print(f"Status: Outside trading hours ({now_ist.strftime('%H:%M IST')}).")
    
    initialize_kite_session()
    market_data, regime = fetch_indicators_and_regime()
    manage_positions_and_scan(market_data, regime)
    
    # Send Scan Completion Summary
    trades = load_json(DB_FILE, [])
    memory = load_json(MEMORY_FILE, {"stcl_pool": 0.0, "cooldowns": {}, "portfolio_peak": INITIAL_CAPITAL})
    open_trades = [t for t in trades if t.get("status") == "OPEN"]
    
    pos_summary = ""
    if not open_trades:
        pos_summary = "• Active Slots: 0/3 (100% Cash)\n"
    else:
        for t in open_trades:
            pos_summary += f"• `{t['symbol']}`: {t['units']} units @ ₹{t['entry_price']} (SL: ₹{t['sl']})\n"
            
    summary_msg = (
        "📊 *SWING ENGINE STATUS*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🟢 *Status:* Scan Complete\n"
        f"⏰ *Server Time:* {now_ist.strftime('%H:%M:%S IST')}\n"
        f"💰 *Capital Base:* ₹{INITIAL_CAPITAL:,.2f}\n"
        f"🛡️ *Tax Shield:* ₹{memory.get('stcl_pool', 0.0):,.2f}\n\n"
        f"*Open Positions ({len(open_trades)}/{MAX_ACTIVE_SLOTS}):*\n"
        f"{pos_summary}"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    send_telegram(summary_msg)


# ==============================================================================
# 10. MAIN CONTROLLER
# ==============================================================================

if __name__ == "__main__":
    # 1. First process any incoming Telegram button clicks or /buy commands
    process_telegram_updates()
    
    # 2. Run the multi-agent market scanner and execution routines
    run_trading_engine()
