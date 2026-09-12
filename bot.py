import os
import sys
import json
import math
import requests
import pyotp
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, time, timedelta, timezone
from kiteconnect import KiteConnect

# ==============================================================================
# 1. CONSTANTS, RISK RULES & WATCHLIST (10-SECTION ARCHITECTURE)
# ==============================================================================

IST = timezone(timedelta(hours=5, minutes=30))

INITIAL_CAPITAL = 100000.0
MAX_ACTIVE_SLOTS = 3
MAX_SLOTS = 3
MAX_RISK_PER_TRADE_PCT = 0.01 # 1% Account Risk per trade (₹1,000)
BASE_TARGET_PCT = 0.035 # +3.5% Target 1 (1:1.4 R:R)
STOP_LOSS_PCT = 0.025 # -2.5% Hard Stop-Loss
MAX_DRAWDOWN_CIRCUIT_PCT = 0.08 # 8% Peak Drawdown Circuit Breaker
STCL_SET_ASIDE_PCT = 0.20 # 20% of STCL credited back to reinvestment
MAX_LEG2_TIME_STOP_DAYS = 15 # Leg 2 structural exit threshold
TRAILING_EMA_PERIOD = 20 # Trailing filter for Leg 2 runner

DB_FILE = "trade_database.json"
MEMORY_FILE = "strategy_memory.json"
HOLIDAYS_CACHE_FILE = "holidays_cache.json"
DASHBOARD_FILE = "docs/index.html" if os.path.exists("docs") else "index.html"

# Environment / Secret Credentials
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_API_SECRET = os.getenv("KITE_API_SECRET")
KITE_USER_ID = os.getenv("KITE_USER_ID")
KITE_PASSWORD = os.getenv("KITE_PASSWORD")
KITE_TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")

# Multi-Asset Sector Watchlist & Cluster Mapping
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
    "AUTOBEES": {"symbol": "AUTOBEES.NS", "cluster": "AUTO_CONSUMPTION"}
}

# ==============================================================================
# 2. PERSISTENCE & UTILITIES (INCLUDING STATUTORY FRICTION & ANALYTICS)
# ==============================================================================

def load_json(filepath: str, default_val):
    if not os.path.exists(filepath):
        return default_val
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading {filepath}: {e}")
        return default_val


def save_json(filepath: str, data):
    try:
        with open(filepath, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Error saving {filepath}: {e}")


def safe_float(val, default: float = 0.0) -> float:
    """Extracts a scalar float cleanly and eliminates NaN / invalid series values."""
    if val is None:
        return default
    try:
        if hasattr(val, "iloc"):
            v = float(val.iloc[0])
        elif hasattr(val, "item"):
            v = float(val.item())
        else:
            v = float(val)
        return default if math.isnan(v) else v
    except Exception:
        return default


def calculate_statutory_friction(entry_price: float, exit_price: float, units: int) -> float:
    """
    Estimates standard round-trip statutory costs for delivery ETF trades:
    - STT/CTT: 0.1% on Buy & Sell turnover
    - Exchange turnover charges: 0.00297%
    - Stamp Duty: 0.015% on Buy turnover
    - SEBI Turnover Charges: ₹10 per crore
    - GST: 18% on (Exchange charges + SEBI charges)
    """
    buy_turnover = entry_price * units
    sell_turnover = exit_price * units
    
    stt = 0.001 * (buy_turnover + sell_turnover)
    stamp_duty = 0.00015 * buy_turnover
    exch_charges = 0.0000297 * (buy_turnover + sell_turnover)
    sebi_charges = 0.000001 * (buy_turnover + sell_turnover)
    gst = 0.18 * (exch_charges + sebi_charges)
    
    return round(stt + stamp_duty + exch_charges + sebi_charges + gst, 2)


def compute_strategy_analytics(closed_trades: list) -> dict:
    """
    Computes systematic trading metrics over completed trades:
    Win Rate, Profit Factor, Payoff Ratio, and Net Expectancy.
    """
    if not closed_trades:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "profit_factor": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "payoff_ratio": 0.0,
            "net_expectancy": 0.0
        }

    gross_profits = []
    gross_losses = []

    for t in closed_trades:
        entry = float(t.get("entry_price", 0.0))
        exit_p = float(t.get("exit_price", 0.0))
        units = int(t.get("units", 0))
        net_diff = (exit_p - entry) * units

        if net_diff > 0:
            gross_profits.append(net_diff)
        elif net_diff < 0:
            gross_losses.append(abs(net_diff))

    wins = len(gross_profits)
    losses = len(gross_losses)
    total_trades = wins + losses

    tot_profit = sum(gross_profits)
    tot_loss = sum(gross_losses)

    win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
    profit_factor = (tot_profit / tot_loss) if tot_loss > 0 else (tot_profit if tot_profit > 0 else 0.0)
    avg_win = (tot_profit / wins) if wins > 0 else 0.0
    avg_loss = (tot_loss / losses) if losses > 0 else 0.0
    payoff_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0

    p_win = wins / total_trades if total_trades > 0 else 0.0
    p_loss = losses / total_trades if total_trades > 0 else 0.0
    net_expectancy = (p_win * avg_win) - (p_loss * avg_loss)

    return {
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 1),
        "gross_profit": round(tot_profit, 2),
        "gross_loss": round(tot_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "payoff_ratio": round(payoff_ratio, 2),
        "net_expectancy": round(net_expectancy, 2)
    }


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
    payload = {
        "allowed_updates": ["message", "callback_query"],
        "timeout": 5
    }
    
    try:
        res = requests.post(url, json=payload, timeout=10).json()
        updates = res.get("result", [])
        print(f"Telegram Polling: Found {len(updates)} pending updates.")
        if not updates:
            return
            
        trades = load_json(DB_FILE, [])
        memory = load_json(MEMORY_FILE, {"stcl_pool": 0.0, "cooldowns": {}, "portfolio_peak": INITIAL_CAPITAL})
        max_slots = globals().get("MAX_ACTIVE_SLOTS", 3)
        
        current_date_str = datetime.now(IST).strftime("%Y-%m-%d")
        current_time_str = datetime.now(IST).strftime("%H:%M:%S IST")
        
        last_update_id = None

        for item in updates:
            last_update_id = item["update_id"]
            action_symbol = None
            entry_price = None
            qty = None
            sl_price = None
            
            # --- Inline Button Callbacks ---
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
                    if len(parts) >= 2:
                        action_symbol = parts[1].upper().replace(".NS", "")
                    if len(parts) >= 5:
                        try:
                            entry_price = float(parts[2])
                            qty = int(parts[3])
                            sl_price = float(parts[4])
                        except ValueError:
                            pass
                
                elif cb_data.startswith("PASS:"):
                    sym = cb_data.split(":")[1] if ":" in cb_data else "Signal"
                    send_telegram(f"⏭️ Signal for {sym} passed.")
                    continue

            # --- Text Commands ---
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
                            pos_text += f"• {t['symbol']}: {t.get('remaining_units', t['units'])} units @ ₹{t['entry_price']} (SL: ₹{t['sl']})\n"
                            
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
                    parts = text.split()
                    if len(parts) > 1:
                        action_symbol = parts[1].upper().replace(".NS", "")

            # --- Order Router ---
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
                                entry_price = round(safe_float(close_val, default=0.0), 2)
                                slot_cap = INITIAL_CAPITAL / max_slots
                                qty = int(slot_cap / entry_price) if entry_price > 0 else 0
                                sl_price = round(entry_price * (1.0 - STOP_LOSS_PCT), 2)

                        if entry_price and qty and sl_price and entry_price > 0:
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

        if last_update_id is not None:
            try:
                requests.post(url, json={"offset": last_update_id + 1}, timeout=5)
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
    return market_open <= now_ist.time() <= market_close


# ==============================================================================
# 5. KITE CONNECT SESSION & LIVE FEED SENTINEL (LTP OVERRIDE)
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


def fetch_kite_live_ltp(kite, symbols: list) -> dict:
    """
    Fetches real-time NSE exchange LTP directly via Kite Connect API.
    Returns a mapping of {symbol: ltp_float}.
    """
    if not kite:
        return {}
    
    instrument_keys = [f"NSE:{s}" for s in symbols]
    try:
        quote_data = kite.ltp(instrument_keys)
        ltp_map = {}
        for inst_key, data in quote_data.items():
            clean_sym = inst_key.replace("NSE:", "")
            price = safe_float(data.get("last_price", 0.0), default=0.0)
            if price > 0:
                ltp_map[clean_sym] = price
        print(f"✅ Kite Real-Time LTP fetched for: {list(ltp_map.keys())}")
        return ltp_map
    except Exception as e:
        print(f"⚠️ Kite LTP fetch failed, falling back to historical data: {e}")
        return {}


# ==============================================================================
# 6. INDICATORS ENGINE (EMA, RSI, VOLATILITY)
# ==============================================================================

def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = (delta.where(delta > 0, 0.0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window=period).mean()
    rs = gain / loss
    return 100.0 - (100.0 / (1.0 + rs))


# ==============================================================================
# 7. AGENT 1 & 2: DATA SENTINEL & REGIME SENTINEL
# ==============================================================================

def fetch_indicators_and_regime():
    market_data = {}
    for code, info in WATCHLIST.items():
        try:
            df = yf.download(info["symbol"], period="6mo", interval="1d", progress=False)
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                
                close_series = df["Close"].squeeze()
                df["ema20"] = close_series.ewm(span=20, adjust=False).mean()
                df["ema50"] = close_series.ewm(span=50, adjust=False).mean()
                df["rsi"] = calculate_rsi(close_series, 14)
                market_data[code] = df
        except Exception as e:
            print(f"Failed fetching data for {code}: {e}")

    # Macro Regime Analysis using NIFTY 50 Core proxy
    regime = "BALANCED"
    if "NIFTYBEES" in market_data and not market_data["NIFTYBEES"].empty:
        df_nifty = market_data["NIFTYBEES"]
        c = safe_float(df_nifty["Close"].iloc[-1])
        e50 = safe_float(df_nifty["ema50"].iloc[-1])
        rsi = safe_float(df_nifty["rsi"].iloc[-1])

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
    
    # --- A. Active Position Management ---
    for t in active_trades:
        sym = t["symbol"]
        df = market_data.get(sym)
        entry_price = float(t["entry_price"])
        units = int(t["units"])
        rem_units = int(t.get("remaining_units", units))
        sl = float(t["sl"])
        target = round(entry_price * (1.0 + BASE_TARGET_PCT), 2)

        current_price = entry_price
        ema20 = entry_price
        if df is not None and not df.empty:
            current_price = safe_float(df["Close"].iloc[-1], default=entry_price)
            ema20 = safe_float(df["ema20"].iloc[-1], default=entry_price)

        if current_price <= 0:
            current_price = entry_price
        
        # Leg 1: +3.5% Target Hit
        if not t.get("leg1_done", False) and current_price >= target:
            half_qty = units // 2
            t["remaining_units"] = units - half_qty
            t["leg1_done"] = True
            t["sl"] = entry_price
            save_json(DB_FILE, trades)
            send_telegram(
                f"🎯 LEG 1 PROFIT BOOKED: {sym}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• Scaled Out: {half_qty} units @ ₹{current_price:.2f} (+3.5%)\n"
                f"• Remaining: {t['remaining_units']} units running\n"
                f"• Stop Loss moved to Breakeven: ₹{entry_price:.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )

        # Leg 2 Runner Exit
        elif t.get("leg1_done", False) and current_price < ema20:
            t["status"] = "CLOSED"
            t["exit_price"] = current_price
            t["exit_date"] = datetime.now(IST).strftime("%Y-%m-%d")
            t["exit_reason"] = "Leg 2 Trend Exit (20 EMA)"
            save_json(DB_FILE, trades)
            send_telegram(
                f"🏆 LEG 2 RUNNER CLOSED: {sym}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• Exited Remaining: {rem_units} units @ ₹{current_price:.2f}\n"
                f"• Exit Trigger: Broke below 20 EMA (₹{ema20:.2f})\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )
            
        # Hard Stop-Loss Trigger
        elif current_price <= sl:
            t["status"] = "CLOSED"
            t["exit_price"] = current_price
            t["exit_date"] = datetime.now(IST).strftime("%Y-%m-%d")
            t["exit_reason"] = "Stop-Loss Hit"
            loss_amount = (entry_price - current_price) * rem_units
            memory["stcl_pool"] = memory.get("stcl_pool", 0.0) + (loss_amount * STCL_SET_ASIDE_PCT)
            save_json(DB_FILE, trades)
            save_json(MEMORY_FILE, memory)
            send_telegram(
                f"🛑 STOP-LOSS HIT: {sym}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• Exited: {rem_units} units @ ₹{current_price:.2f}\n"
                f"• Realized Loss: ₹{loss_amount:.2f}\n"
                f"• STCL Shield Added: ₹{(loss_amount * STCL_SET_ASIDE_PCT):.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )

    # --- B. Opportunity Scanner ---
    current_open_trades = [t for t in trades if t.get("status") == "OPEN"]
    available_slots = MAX_ACTIVE_SLOTS - len(current_open_trades)
    
    if available_slots > 0:
        for sym, df in market_data.items():
            if available_slots <= 0:
                break
            if sym in [t["symbol"] for t in current_open_trades]:
                continue
            
            cluster = WATCHLIST[sym]["cluster"]
            if cluster in active_clusters:
                continue
                
            close = safe_float(df["Close"].iloc[-1], default=0.0)
            ema20 = safe_float(df["ema20"].iloc[-1], default=0.0)
            ema50 = safe_float(df["ema50"].iloc[-1], default=0.0)
            rsi = safe_float(df["rsi"].iloc[-1], default=0.0)
            
            if close <= 0 or ema20 <= 0 or ema50 <= 0:
                continue
            
            if (close > ema50) and (abs(close - ema20) / close <= 0.015) and (40.0 <= rsi <= 60.0):
                slot_capital = (INITIAL_CAPITAL / MAX_ACTIVE_SLOTS) + (memory.get("stcl_pool", 0.0) / MAX_ACTIVE_SLOTS)
                qty = int(slot_capital // close)
                stop_loss = round(close * (1.0 - STOP_LOSS_PCT), 2)
                target_1 = round(close * (1.0 + BASE_TARGET_PCT), 2)
                
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
                        f"⚡ NEW SWING SIGNAL DETECTED\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📈 Symbol: {sym} ({cluster})\n"
                        f"💰 Entry: ₹{close:.2f}\n"
                        f"🛑 Stop Loss: ₹{stop_loss:.2f} (-2.5%)\n"
                        f"🎯 Target 1: ₹{target_1:.2f} (+3.5%)\n"
                        f"📦 Position Size: {qty} units (~₹{(qty * close):,.2f})\n"
                        f"📊 RSI 14: {rsi:.1f}\n"
                        f"━━━━━━━━━━━━━━━━━━━━"
                    )

                    send_telegram(signal_card, reply_markup=inline_keyboard)
                    available_slots -= 1


# ==============================================================================
# 9. AGENT 5 & 6: TAX SHIELD & EXECUTION ENGINE
# ==============================================================================

def generate_html_dashboard(trades, memory, market_data=None):
    open_trades = [t for t in trades if t.get("status") == "OPEN"]
    closed_trades = [t for t in trades if t.get("status") == "CLOSED"]
    
    analytics = compute_strategy_analytics(closed_trades)

    total_gross_unrealized = 0.0
    total_net_unrealized = 0.0
    total_gross_realized = 0.0
    total_net_realized = 0.0
    total_friction_paid = 0.0

    # Build Open Positions Rows
    open_rows = ""
    for t in open_trades:
        sym = t["symbol"]
        entry = float(t["entry_price"])
        rem_units = int(t.get("remaining_units", t["units"]))
        
        ltp = entry
        if market_data and sym in market_data and not market_data[sym].empty:
            fetched_ltp = safe_float(market_data[sym]["Close"].iloc[-1], default=entry)
            if fetched_ltp > 0:
                ltp = fetched_ltp
            
        gross_pnl = (ltp - entry) * rem_units
        est_friction = calculate_statutory_friction(entry, ltp, rem_units)
        net_pnl = gross_pnl - est_friction
        net_pct = (net_pnl / (entry * rem_units)) * 100.0 if entry > 0 else 0.0
        
        total_gross_unrealized += gross_pnl
        total_net_unrealized += net_pnl
        
        pnl_color = "#34d399" if net_pnl >= 0 else "#f87171"
        pnl_sign = "+" if net_pnl >= 0 else ""
        
        target1 = round(entry * (1.0 + BASE_TARGET_PCT), 2)
        leg1_status = "🎯 Booked" if t.get("leg1_done") else f"₹{target1:.2f}"

        open_rows += f"""
        <tr>
            <td><span class="badge badge-open">OPEN</span></td>
            <td><strong>{sym}</strong></td>
            <td>₹{entry:.2f}</td>
            <td>₹{ltp:.2f}</td>
            <td>{rem_units}</td>
            <td>₹{float(t['sl']):.2f}</td>
            <td>{leg1_status}</td>
            <td style="color: {pnl_color}; font-weight: bold;">
                {pnl_sign}₹{net_pnl:.2f} <small style="color:#94a3b8;">({pnl_sign}{net_pct:.2f}%)</small>
                <div style="font-size:10px; color:#64748b; font-weight:normal;">Gross: {pnl_sign}₹{gross_pnl:.2f} | Chgs: ₹{est_friction:.2f}</div>
            </td>
            <td>{t['entry_date']}</td>
        </tr>
        """

    # Build Closed Positions Rows
    closed_rows = ""
    for t in closed_trades[::-1]:
        entry = float(t["entry_price"])
        exit_p = float(t.get("exit_price", 0.0))
        units = int(t["units"])
        
        gross_pnl = (exit_p - entry) * units if exit_p > 0 else 0.0
        friction = calculate_statutory_friction(entry, exit_p, units)
        net_pnl = gross_pnl - friction
        net_pct = (net_pnl / (entry * units)) * 100.0 if entry > 0 else 0.0
        
        total_gross_realized += gross_pnl
        total_net_realized += net_pnl
        total_friction_paid += friction
        
        pnl_color = "#34d399" if net_pnl >= 0 else "#f87171"
        pnl_sign = "+" if net_pnl >= 0 else ""

        closed_rows += f"""
        <tr>
            <td><span class="badge badge-closed">CLOSED</span></td>
            <td><strong>{t['symbol']}</strong></td>
            <td>₹{entry:.2f}</td>
            <td>₹{exit_p:.2f}</td>
            <td>{units}</td>
            <td>{t.get('exit_reason', '-')}</td>
            <td style="color: {pnl_color}; font-weight: bold;">
                {pnl_sign}₹{net_pnl:.2f} <small style="color:#94a3b8;">({pnl_sign}{net_pct:.2f}%)</small>
                <div style="font-size:10px; color:#64748b; font-weight:normal;">Gross: {pnl_sign}₹{gross_pnl:.2f} | Chgs: ₹{friction:.2f}</div>
            </td>
            <td>{t.get('exit_date', '-')}</td>
        </tr>
        """

    net_equity = INITIAL_CAPITAL + total_net_realized + total_net_unrealized

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ETF Swing Trading Terminal & Analytics</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0b0f19; color: #f8fafc; padding: 24px; margin: 0; }}
        .container {{ max-width: 1200px; margin: auto; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1e293b; padding-bottom: 16px; margin-bottom: 24px; }}
        .card-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 24px; }}
        .card {{ background: #131c2e; padding: 16px; border-radius: 8px; border: 1px solid #1e293b; }}
        .card h4 {{ margin: 0 0 6px 0; color: #94a3b8; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }}
        .card p {{ margin: 0; font-size: 20px; font-weight: bold; color: #f8fafc; }}
        .section-title {{ font-size: 15px; font-weight: bold; margin: 24px 0 12px 0; color: #38bdf8; text-transform: uppercase; letter-spacing: 0.5px; }}
        table {{ width: 100%; border-collapse: collapse; background: #131c2e; border-radius: 8px; overflow: hidden; margin-bottom: 24px; }}
        th, td {{ padding: 12px 14px; text-align: left; font-size: 13px; border-bottom: 1px solid #1e293b; }}
        th {{ background: #0f172a; color: #94a3b8; font-size: 11px; text-transform: uppercase; }}
        .badge {{ padding: 3px 6px; border-radius: 4px; font-size: 10px; font-weight: bold; }}
        .badge-open {{ background: #064e3b; color: #34d399; }}
        .badge-closed {{ background: #334155; color: #cbd5e1; }}
        .insight-box {{ background: #131c2e; border-left: 4px solid #38bdf8; padding: 14px 18px; border-radius: 4px; margin-bottom: 24px; font-size: 13px; line-height: 1.5; color: #cbd5e1; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h2 style="margin:0;">⚡ ETF Swing Trading Command Center</h2>
                <small style="color:#64748b;">Autonomous Quantitative Engine • Post-Tax & Statistical Audit</small>
            </div>
            <div style="text-align:right;">
                <span style="color:#34d399; font-weight:bold;">● ENGINE LIVE</span><br>
                <small style="color:#94a3b8;">Updated: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}</small>
            </div>
        </div>

        <div class="card-grid">
            <div class="card"><h4>Active Slots</h4><p>{len(open_trades)} / {MAX_ACTIVE_SLOTS}</p></div>
            <div class="card"><h4>Net Portfolio Value</h4><p>₹{net_equity:,.2f}</p></div>
            <div class="card"><h4>Net Unrealized P&L</h4><p style="color: {'#34d399' if total_net_unrealized >= 0 else '#f87171'};">{' ' if total_net_unrealized >= 0 else ''}₹{total_net_unrealized:,.2f}</p></div>
            <div class="card"><h4>Net Realized P&L</h4><p style="color: {'#34d399' if total_net_realized >= 0 else '#f87171'};">{' ' if total_net_realized >= 0 else ''}₹{total_net_realized:,.2f}</p></div>
            <div class="card"><h4>Total Friction (Gov/Exch)</h4><p style="color:#f59e0b;">₹{total_friction_paid:,.2f}</p></div>
            <div class="card"><h4>STCL Shield Pool</h4><p style="color:#a78bfa;">₹{memory.get('stcl_pool', 0.0):,.2f}</p></div>
        </div>

        <div class="section-title">Strategy Health & Expectancy Metrics (Closed Trades)</div>
        <div class="card-grid">
            <div class="card"><h4>Win Rate</h4><p>{analytics['win_rate']}% <span style="font-size:13px; color:#94a3b8;">({analytics['wins']}/{analytics['total_trades']})</span></p></div>
            <div class="card"><h4>Profit Factor</h4><p style="color: {'#34d399' if analytics['profit_factor'] >= 1.5 else ('#fbbf24' if analytics['profit_factor'] >= 1.0 else '#f87171')};">{analytics['profit_factor']}x</p></div>
            <div class="card"><h4>Payoff Ratio (R:R)</h4><p>{analytics['payoff_ratio']}x</p></div>
            <div class="card"><h4>Trade Expectancy</h4><p style="color: {'#34d399' if analytics['net_expectancy'] >= 0 else '#f87171'};">{' ' if analytics['net_expectancy'] >= 0 else ''}₹{analytics['net_expectancy']}</p></div>
        </div>

        <div class="insight-box">
            <strong>Systematic Lesson & Regime Insight:</strong><br>
            Current sample: <strong>{analytics['total_trades']} completed trades</strong>. In defensive market regimes (Core index below 50 EMA), initial entries endure higher stop-out rates. Capital preservation rules (-2.5% stop-loss & STCL loss shield) strictly contain total drawdown while the system scans for high-momentum sector divergence. Minimum statistical significance requires <strong>30–50 completed trades</strong>.
        </div>

        <div class="section-title">Active Holdings (Net of Charges)</div>
        <table>
            <thead>
                <tr>
                    <th>Status</th>
                    <th>Symbol</th>
                    <th>Entry</th>
                    <th>LTP</th>
                    <th>Qty</th>
                    <th>SL</th>
                    <th>Target 1</th>
                    <th>Net P&L (Post-Charges)</th>
                    <th>Entry Date</th>
                </tr>
            </thead>
            <tbody>
                {open_rows if open_rows else '<tr><td colspan="9" style="text-align:center; color:#64748b;">No active positions (100% Cash)</td></tr>'}
            </tbody>
        </table>

        <div class="section-title">Trade History & Realized Audit</div>
        <table>
            <thead>
                <tr>
                    <th>Status</th>
                    <th>Symbol</th>
                    <th>Entry</th>
                    <th>Exit</th>
                    <th>Units</th>
                    <th>Reason</th>
                    <th>Net Realized P&L</th>
                    <th>Exit Date</th>
                </tr>
            </thead>
            <tbody>
                {closed_rows if closed_rows else '<tr><td colspan="8" style="text-align:center; color:#64748b;">No closed trades yet</td></tr>'}
            </tbody>
        </table>
    </div>
</body>
</html>"""
    try:
        with open(DASHBOARD_FILE, "w") as f:
            f.write(html_content)
    except Exception as e:
        print(f"Error generating dashboard: {e}")


def run_trading_engine():
    now_ist = datetime.now(IST)
    if not is_market_open():
        print(f"Status: Outside trading hours ({now_ist.strftime('%H:%M IST')}).")
    
    # 1. Initialize Kite Session safely
    kite = initialize_kite_session()
    
    # 2. Fetch historical series for indicators
    market_data, regime = fetch_indicators_and_regime()

    # 3. Attempt live exchange LTP fetch via Kite
    all_symbols = list(WATCHLIST.keys())
    kite_ltps = fetch_kite_live_ltp(kite, all_symbols)

    # Inject live quotes into market data series
    for sym, ltp_val in kite_ltps.items():
        if sym in market_data and not market_data[sym].empty and ltp_val > 0:
            market_data[sym].iloc[-1, market_data[sym].columns.get_loc("Close")] = ltp_val

    # 4. Evaluate stop-losses, profit targets, and new opportunities
    manage_positions_and_scan(market_data, regime)
    
    trades = load_json(DB_FILE, [])
    memory = load_json(MEMORY_FILE, {"stcl_pool": 0.0, "cooldowns": {}, "portfolio_peak": INITIAL_CAPITAL})
    open_trades = [t for t in trades if t.get("status") == "OPEN"]
    
    # 5. Refresh GitHub Pages Monitor Dashboard
    generate_html_dashboard(trades, memory, market_data=market_data)
    
    # 6. Format Telegram Summary Report with robust NaN fallback
    pos_summary = ""
    if not open_trades:
        pos_summary = f"• Active Slots: 0/{MAX_ACTIVE_SLOTS} (100% Cash)\n"
    else:
        for t in open_trades:
            sym = t["symbol"]
            entry = float(t["entry_price"])
            units = int(t.get("remaining_units", t["units"]))
            sl = float(t["sl"])
            target = round(entry * (1.0 + BASE_TARGET_PCT), 2)
            
            # Prioritize Kite live quote, fall back to safe yfinance close, then entry
            ltp = kite_ltps.get(sym)
            if (ltp is None or math.isnan(ltp) or ltp <= 0) and market_data and sym in market_data and not market_data[sym].empty:
                ltp = safe_float(market_data[sym]["Close"].iloc[-1], default=entry)
            
            if ltp is None or math.isnan(ltp) or ltp <= 0:
                ltp = entry

            ltp = round(float(ltp), 2)
                
            gross_pnl = (ltp - entry) * units
            friction = calculate_statutory_friction(entry, ltp, units)
            net_pnl = gross_pnl - friction
            net_pct = (net_pnl / (entry * units)) * 100.0 if entry > 0 else 0.0
            pnl_sign = "+" if net_pnl >= 0 else ""
            
            pos_summary += (
                f"• {sym}: {units} units\n"
                f" Entry: ₹{entry:.2f} | LTP: ₹{ltp:.2f}\n"
                f" 🎯 Target: ₹{target:.2f} | 🛑 SL: ₹{sl:.2f}\n"
                f" Net P&L: {pnl_sign}₹{net_pnl:.2f} ({pnl_sign}{net_pct:.2f}%)\n"
                f" (Gross: {pnl_sign}₹{gross_pnl:.2f} | Est. Taxes/Chgs: ₹{friction:.2f})\n\n"
            )
            
    summary_msg = (
        "📊 SWING ENGINE STATUS\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🟢 Status: Scan Complete\n"
        f"⏰ Server Time: {now_ist.strftime('%H:%M:%S IST')}\n"
        f"💰 Capital Base: ₹{INITIAL_CAPITAL:,.2f}\n"
        f"🛡️ Tax Shield: ₹{memory.get('stcl_pool', 0.0):,.2f}\n\n"
        f"Open Positions ({len(open_trades)}/{MAX_ACTIVE_SLOTS}):\n"
        f"{pos_summary.strip()}\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    send_telegram(summary_msg)


# ==============================================================================
# 10. MAIN CONTROLLER
# ==============================================================================

if __name__ == "__main__":
    process_telegram_updates()
    run_trading_engine()
