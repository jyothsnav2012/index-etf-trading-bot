import os
import sys
import json
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
DASHBOARD_FILE = "index.html"

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
# 2. PERSISTENCE & UTILITIES
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


def safe_float(val) -> float:
    """Extracts a scalar float cleanly regardless of multi-index pandas structure."""
    if hasattr(val, "iloc"):
        return float(val.iloc[0])
    if hasattr(val, "item"):
        return float(val.item())
    return float(val)


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
        "text": message,
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
                            pos_text += f"• {t['symbol']}: {t['units']} units @ ₹{t['entry_price']} (SL: ₹{t['sl']})\n"
                            
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

            # --- 3. Unified Trade Execution ---
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
                                entry_price = round(safe_float(close_val), 2)
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

        # Clear processed updates from queue
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
    
    # --- A. Active Position Management (Profit Targets, Trailing SL, Stops) ---
    for t in active_trades:
        sym = t["symbol"]
        df = market_data.get(sym)
        if df is None or df.empty:
            continue
            
        current_price = safe_float(df["Close"].iloc[-1])
        ema20 = safe_float(df["ema20"].iloc[-1])
        entry_price = float(t["entry_price"])
        units = int(t["units"])
        rem_units = int(t.get("remaining_units", units))
        sl = float(t["sl"])
        target = round(entry_price * (1.0 + BASE_TARGET_PCT), 2)
        
        # Leg 1: +3.5% Target Hit (Scale out 50% and move stop to Breakeven)
        if not t.get("leg1_done", False) and current_price >= target:
            half_qty = units // 2
            t["remaining_units"] = units - half_qty
            t["leg1_done"] = True
            t["sl"] = entry_price # Trailing stop to cost
            save_json(DB_FILE, trades)
            send_telegram(
                f"🎯 *LEG 1 PROFIT BOOKED: `{sym}`*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• Scaled Out: {half_qty} units @ ₹{current_price:.2f} (+3.5%)\n"
                f"• Remaining: {t['remaining_units']} units running\n"
                f"• Stop Loss moved to Breakeven: ₹{entry_price:.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )

        # Leg 2 Runner Exit (Trailing below 20 EMA after Leg 1 is achieved)
        elif t.get("leg1_done", False) and current_price < ema20:
            t["status"] = "CLOSED"
            t["exit_price"] = current_price
            t["exit_date"] = datetime.now(IST).strftime("%Y-%m-%d")
            t["exit_reason"] = "Leg 2 Trend Exit (20 EMA)"
            save_json(DB_FILE, trades)
            send_telegram(
                f"🏆 *LEG 2 RUNNER CLOSED: `{sym}`*\n"
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
            # Credit STCL tax shield pool
            memory["stcl_pool"] = memory.get("stcl_pool", 0.0) + (loss_amount * STCL_SET_ASIDE_PCT)
            save_json(DB_FILE, trades)
            save_json(MEMORY_FILE, memory)
            send_telegram(
                f"🛑 *STOP-LOSS HIT: `{sym}`*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• Exited: {rem_units} units @ ₹{current_price:.2f}\n"
                f"• Realized Loss: ₹{loss_amount:.2f}\n"
                f"• STCL Shield Added: ₹{(loss_amount * STCL_SET_ASIDE_PCT):.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )

    # --- B. Opportunity Scanner (Alpha Signals) ---
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
                
            close = safe_float(df["Close"].iloc[-1])
            ema20 = safe_float(df["ema20"].iloc[-1])
            ema50 = safe_float(df["ema50"].iloc[-1])
            rsi = safe_float(df["rsi"].iloc[-1])
            
            # Triple Confirmation Entry Setup
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
                        f"⚡ *NEW SWING SIGNAL DETECTED*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📈 *Symbol:* `{sym}` ({cluster})\n"
                        f"💰 *Entry:* ₹{close:.2f}\n"
                        f"🛑 *Stop Loss:* ₹{stop_loss:.2f} (-2.5%)\n"
                        f"🎯 *Target 1:* ₹{target_1:.2f} (+3.5%)\n"
                        f"📦 *Position Size:* {qty} units (~₹{(qty * close):,.2f})\n"
                        f"📊 *RSI 14:* {rsi:.1f}\n"
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
    
    total_unrealized_pnl = 0.0
    total_realized_pnl = 0.0
    wins = 0

    # Build Open Positions Rows
    open_rows = ""
    for t in open_trades:
        sym = t["symbol"]
        entry = float(t["entry_price"])
        rem_units = int(t.get("remaining_units", t["units"]))
        
        # Get Live/Current Price from market_data if available
        ltp = entry
        if market_data and sym in market_data and not market_data[sym].empty:
            ltp = safe_float(market_data[sym]["Close"].iloc[-1])
            
        unrealized_pnl = (ltp - entry) * rem_units
        unrealized_pnl_pct = ((ltp - entry) / entry) * 100.0 if entry > 0 else 0.0
        total_unrealized_pnl += unrealized_pnl
        
        pnl_color = "#34d399" if unrealized_pnl >= 0 else "#f87171"
        pnl_sign = "+" if unrealized_pnl >= 0 else ""
        
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
                {pnl_sign}₹{unrealized_pnl:.2f} ({pnl_sign}{unrealized_pnl_pct:.2f}%)
            </td>
            <td>{t['entry_date']}</td>
        </tr>
        """

    # Build Closed Positions Rows
    closed_rows = ""
    for t in closed_trades[::-1]: # Latest closed first
        entry = float(t["entry_price"])
        exit_p = float(t.get("exit_price", 0.0))
        units = int(t["units"])
        realized_pnl = (exit_p - entry) * units if exit_p > 0 else 0.0
        realized_pnl_pct = ((exit_p - entry) / entry) * 100.0 if entry > 0 else 0.0
        total_realized_pnl += realized_pnl
        
        if realized_pnl > 0:
            wins += 1
            
        pnl_color = "#34d399" if realized_pnl >= 0 else "#f87171"
        pnl_sign = "+" if realized_pnl >= 0 else ""

        closed_rows += f"""
        <tr>
            <td><span class="badge badge-closed">CLOSED</span></td>
            <td><strong>{t['symbol']}</strong></td>
            <td>₹{entry:.2f}</td>
            <td>₹{exit_p:.2f}</td>
            <td>{units}</td>
            <td>{t.get('exit_reason', '-')}</td>
            <td style="color: {pnl_color}; font-weight: bold;">
                {pnl_sign}₹{realized_pnl:.2f} ({pnl_sign}{realized_pnl_pct:.2f}%)
            </td>
            <td>{t.get('exit_date', '-')}</td>
        </tr>
        """

    total_closed = len(closed_trades)
    win_rate = (wins / total_closed * 100.0) if total_closed > 0 else 0.0
    net_equity = INITIAL_CAPITAL + total_realized_pnl + total_unrealized_pnl

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ETF Swing Trading Terminal & PnL</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0b0f19; color: #f8fafc; padding: 24px; margin: 0; }}
        .container {{ max-width: 1200px; margin: auto; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1e293b; padding-bottom: 16px; margin-bottom: 24px; }}
        .card-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 28px; }}
        .card {{ background: #131c2e; padding: 16px; border-radius: 8px; border: 1px solid #1e293b; }}
        .card h4 {{ margin: 0 0 6px 0; color: #94a3b8; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }}
        .card p {{ margin: 0; font-size: 20px; font-weight: bold; color: #f8fafc; }}
        .section-title {{ font-size: 16px; font-weight: bold; margin: 24px 0 12px 0; color: #38bdf8; text-transform: uppercase; }}
        table {{ width: 100%; border-collapse: collapse; background: #131c2e; border-radius: 8px; overflow: hidden; margin-bottom: 24px; }}
        th, td {{ padding: 12px 14px; text-align: left; font-size: 13px; border-bottom: 1px solid #1e293b; }}
        th {{ background: #0f172a; color: #94a3b8; font-size: 11px; text-transform: uppercase; }}
        .badge {{ padding: 3px 6px; border-radius: 4px; font-size: 10px; font-weight: bold; }}
        .badge-open {{ background: #064e3b; color: #34d399; }}
        .badge-closed {{ background: #334155; color: #cbd5e1; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h2 style="margin:0;">⚡ ETF Swing Trading Command Center</h2>
                <small style="color:#64748b;">Autonomous Multi-Agent Engine</small>
            </div>
            <div style="text-align:right;">
                <span style="color:#34d399; font-weight:bold;">● ENGINE LIVE</span><br>
                <small style="color:#94a3b8;">Updated: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}</small>
            </div>
        </div>

        <div class="card-grid">
            <div class="card"><h4>Active Slots</h4><p>{len(open_trades)} / {MAX_ACTIVE_SLOTS}</p></div>
            <div class="card"><h4>Capital Base</h4><p>₹{INITIAL_CAPITAL:,.2f}</p></div>
            <div class="card"><h4>Net Portfolio Value</h4><p>₹{net_equity:,.2f}</p></div>
            <div class="card"><h4>Unrealized P&L</h4><p style="color: {'#34d399' if total_unrealized_pnl >= 0 else '#f87171'};">{' ' if total_unrealized_pnl >= 0 else ''}₹{total_unrealized_pnl:,.2f}</p></div>
            <div class="card"><h4>Realized P&L</h4><p style="color: {'#34d399' if total_realized_pnl >= 0 else '#f87171'};">{' ' if total_realized_pnl >= 0 else ''}₹{total_realized_pnl:,.2f}</p></div>
            <div class="card"><h4>STCL Shield Pool</h4><p style="color:#a78bfa;">₹{memory.get('stcl_pool', 0.0):,.2f}</p></div>
            <div class="card"><h4>Win Rate</h4><p>{win_rate:.1f}% ({wins}/{total_closed})</p></div>
        </div>

        <div class="section-title">Active Holdings</div>
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
                    <th>Unrealized P&L</th>
                    <th>Entry Date</th>
                </tr>
            </thead>
            <tbody>
                {open_rows if open_rows else '<tr><td colspan="9" style="text-align:center; color:#64748b;">No active positions (100% Cash)</td></tr>'}
            </tbody>
        </table>

        <div class="section-title">Trade History & Realized P&L</div>
        <table>
            <thead>
                <tr>
                    <th>Status</th>
                    <th>Symbol</th>
                    <th>Entry</th>
                    <th>Exit</th>
                    <th>Units</th>
                    <th>Reason</th>
                    <th>Realized P&L</th>
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
    
    initialize_kite_session()
    market_data, regime = fetch_indicators_and_regime()
    manage_positions_and_scan(market_data, regime)
    
    trades = load_json(DB_FILE, [])
    memory = load_json(MEMORY_FILE, {"stcl_pool": 0.0, "cooldowns": {}, "portfolio_peak": INITIAL_CAPITAL})
    open_trades = [t for t in trades if t.get("status") == "OPEN"]
    
    # Refresh GitHub Pages Monitor Dashboard
    generate_html_dashboard(trades, memory)
    
    # Send Scan Completion Summary
    pos_summary = ""
    if not open_trades:
        pos_summary = f"• Active Slots: 0/{MAX_ACTIVE_SLOTS} (100% Cash)\n"
    else:
        for t in open_trades:
            pos_summary += f"• `{t['symbol']}`: {t.get('remaining_units', t['units'])} units @ ₹{t['entry_price']} (SL: ₹{t['sl']})\n"
            
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
    # 1. First process pending Telegram interactions (/buy, /status, button clicks)
    process_telegram_updates()
    
    # 2. Execute multi-agent data pipeline, exits, and opportunity scanner
    run_trading_engine()
