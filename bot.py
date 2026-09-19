import os
import sys
import json
import time as time_module
from datetime import datetime, time
import pytz
import requests
import yfinance as yf
from kiteconnect import KiteConnect

# ==============================================================================
# 1. CONFIGURATION, ASSET WATCHLIST & STATUTORY PARAMETERS
# ==============================================================================

IST = pytz.timezone("Asia/Kolkata")

# Capital and Risk Management
INITIAL_CAPITAL = 100000.0 # ₹1 Lakh
MAX_ACTIVE_SLOTS = 3
SLOT_CAPITAL = INITIAL_CAPITAL / MAX_ACTIVE_SLOTS
STOP_LOSS_PCT = 0.025 # 2.5% Hard Stop
BASE_TARGET_PCT = 0.035 # 3.5% Target 1 (50% scale-out)
STCL_SET_ASIDE_PCT = 0.20 # 20% Short-Term Capital Loss Tax Shield

# Persistence Paths
DB_FILE = "trade_database.json"
MEMORY_FILE = "agent_memory.json"
DASHBOARD_FILE = "docs/index.html"

# Environment Variables / GitHub Secrets
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN")

# Multi-Asset ETF Watchlist (Decorrelated Clusters)
WATCHLIST = {
    "NIFTYBEES": {"name": "Nifty 50 Core", "cluster": "LARGE_CAP_EQUITY"},
    "JUNIORBEES": {"name": "Nifty Next 50", "cluster": "LARGE_MID_EQUITY"},
    "MID150BEES": {"name": "Nifty Midcap 150", "cluster": "MIDCAP_EQUITY"},
    "GOLDBEES": {"name": "Nippon Gold ETF", "cluster": "COMMODITIES_PRECIOUS"},
    "SILVERBEES": {"name": "Nippon Silver ETF", "cluster": "COMMODITIES_WHITE"},
    "PHARMABEES": {"name": "Nifty Pharma Index", "cluster": "HEALTHCARE_DEFENSIVE"},
    "BANKBEES": {"name": "Nifty Bank Index", "cluster": "FINANCIALS_CYCLICAL"},
    "ITBEES": {"name": "Nifty IT Index", "cluster": "TECHNOLOGY_GROWTH"},
    "CPSEETF": {"name": "CPSE PSE Index", "cluster": "PSU_VALUE"},
    "MON100": {"name": "Motilal Nasdaq 100", "cluster": "GLOBAL_TECH_DOLLAR"}
}

# ==============================================================================
# 2. UTILITY & PERSISTENCE ENGINE
# ==============================================================================

def safe_float(val, default=0.0):
    try:
        if hasattr(val, "item"):
            val = val.item()
        return float(val)
    except (ValueError, TypeError):
        return default

def load_json(filepath, default_data):
    if not os.path.exists(filepath):
        return default_data
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Error reading {filepath}: {e}")
        return default_data

def save_json(filepath, data):
    os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# ==============================================================================
# 3. STATUTORY FRICTION & TAXATION ENGINE (SEBI / STT / GST / STCL)
# ==============================================================================

def calculate_statutory_charges(buy_price: float, sell_price: float, units: int) -> dict:
    """
    Computes exact statutory friction for Delivery Equity/ETF transactions:
    - STT: 0.1% on both Buy and Sell
    - Exchange Turnover Fees: 0.00345%
    - SEBI Charges: ₹10 per crore (0.0001%)
    - Stamp Duty: 0.015% on Buy turnover
    - GST: 18% on (Exchange charges + SEBI charges)
    - Demat DP Charges: ₹0 for ETF buy/sell transactions on Zerodha
    """
    buy_turnover = buy_price * units
    sell_turnover = sell_price * units
    total_turnover = buy_turnover + sell_turnover

    stt = round(0.001 * total_turnover, 2)
    exchange_charges = round(0.0000345 * total_turnover, 2)
    sebi_charges = round(0.000001 * total_turnover, 2)
    stamp_duty = round(0.00015 * buy_turnover, 2)
    gst = round(0.18 * (exchange_charges + sebi_charges), 2)
    total_charges = round(stt + exchange_charges + sebi_charges + stamp_duty + gst, 2)

    gross_pnl = round((sell_price - buy_price) * units, 2)
    net_pnl = round(gross_pnl - total_charges, 2)

    return {
        "buy_turnover": buy_turnover,
        "sell_turnover": sell_turnover,
        "stt": stt,
        "exchange_charges": exchange_charges,
        "sebi_charges": sebi_charges,
        "stamp_duty": stamp_duty,
        "gst": gst,
        "total_charges": total_charges,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl
    }

# ==============================================================================
# 4. MARKET CALENDAR SENTINEL (DYNAMIC TICK-BASED STATUS)
# ==============================================================================

def is_market_open() -> bool:
    """
    Determines market status dynamically without relying on hardcoded holiday lists:
    1. Rejects weekends (Saturday & Sunday).
    2. Rejects timestamps outside 09:15 to 15:30 IST.
    3. Checks live intraday trade tick activity on NIFTYBEES.NS.
    """
    now_ist = datetime.now(IST)

    # 1. Weekend filter
    if now_ist.weekday() >= 5:
        return False

    # 2. Market window filter (09:15 - 15:30 IST)
    market_open = time(9, 15)
    market_close = time(15, 30)
    if not (market_open <= now_ist.time() <= market_close):
        return False

    # 3. Dynamic Intraday Tick Validation via NIFTYBEES
    try:
        nifty = yf.download("NIFTYBEES.NS", period="1d", interval="1m", progress=False)
        if nifty.empty:
            print(f"Sentinel: No intraday trades recorded today ({now_ist.strftime('%Y-%m-%d')}). Holiday detected.")
            return False

        last_trade_time = nifty.index[-1].to_pydatetime()
        if last_trade_time.date() < now_ist.date():
            print(f"Sentinel: Last recorded tick belongs to {last_trade_time.date()}. Market is closed today.")
            return False

        return True
    except Exception as e:
        print(f"⚠️ Dynamic market check notice: {e}. Falling back to open during trading hours.")
        return True

# ==============================================================================
# 5. KITE CONNECT SESSION & LIVE FEED SENTINEL
# ==============================================================================

def initialize_kite_session():
    """
    Initializes Kite session using KITE_ACCESS_TOKEN if provided.
    Runs in clean fallback mode if token is omitted.
    """
    if not (KITE_API_KEY and KITE_ACCESS_TOKEN):
        return None
    try:
        kite = KiteConnect(api_key=KITE_API_KEY.strip())
        kite.set_access_token(KITE_ACCESS_TOKEN.strip())
        kite.profile()
        print("✅ Kite session established with active access token.")
        return kite
    except Exception as e:
        print(f"⚠️ Kite session offline ({e}). Operating on standard market feed.")
        return None

def fetch_kite_live_ltp(kite, symbols: list) -> dict:
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
        print(f"⚠️ Kite LTP fetch bypassed: {e}")
        return {}

# ==============================================================================
# 6. TELEGRAM REPORTING & ON-DEMAND STATUS DISPATCHER
# ==============================================================================

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ℹ️ Telegram credentials missing. Message suppressed.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": str(TELEGRAM_CHAT_ID).strip(),
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        res = requests.post(url, json=payload, timeout=10).json()
        if not res.get("ok"):
            print(f"❌ Telegram Error: {res.get('description')}")
        else:
            print("✅ Telegram notification sent successfully.")
    except Exception as e:
        print(f"⚠️ Telegram dispatch exception: {e}")

def process_telegram_updates():
    """
    Poller for audit and status queries (/status, /pnl).
    Autonomous execution operates without requiring manual /buy intervention.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    payload = {"allowed_updates": ["message"], "timeout": 5}

    try:
        res = requests.post(url, json=payload, timeout=10).json()
        updates = res.get("result", [])
        if not updates:
            return

        print(f"Telegram Polling: Found {len(updates)} pending update(s).")
        trades = load_json(DB_FILE, [])
        memory = load_json(MEMORY_FILE, {"stcl_pool": 0.0, "cooldowns": {}, "portfolio_peak": INITIAL_CAPITAL})
        current_time_str = datetime.now(IST).strftime("%H:%M:%S IST")
        last_update_id = None

        for item in updates:
            last_update_id = item["update_id"]
            if "message" in item:
                msg = item["message"]
                text = msg.get("text", "").strip()

                if text in ["/start", "/status", "/dashboard", "/pnl"]:
                    current_open = [t for t in trades if t.get("status") == "OPEN"]
                    pos_text = ""
                    if not current_open:
                        pos_text = f"• Active Slots: 0/{MAX_ACTIVE_SLOTS} (100% Cash)\n"
                    else:
                        for t in current_open:
                            pos_text += f"• `{t['symbol']}`: {t.get('remaining_units', t['units'])} units @ ₹{t['entry_price']:.2f} (SL: ₹{t['sl']:.2f})\n"

                    status_msg = (
                        "📊 *SWING ENGINE STATUS*\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "🟢 *Status:* Fully Autonomous Engine Active\n"
                        f"⏰ *Server Time:* `{current_time_str}`\n"
                        f"💰 *Capital Base:* ₹{INITIAL_CAPITAL:,.2f}\n"
                        f"🛡️ *Tax Shield:* ₹{memory.get('stcl_pool', 0.0):,.2f}\n\n"
                        f"*Open Positions ({len(current_open)}/{MAX_ACTIVE_SLOTS}):*\n"
                        f"{pos_text}"
                        "━━━━━━━━━━━━━━━━━━━━"
                    )
                    send_telegram(status_msg)

        if last_update_id is not None:
            try:
                requests.post(url, json={"offset": last_update_id + 1}, timeout=5)
            except Exception:
                pass

    except Exception as e:
        print(f"⚠️ Telegram polling error: {e}")

# ==============================================================================
# 7. QUANTITATIVE ANALYSIS & REGIME SENTINEL (EMA / RSI)
# ==============================================================================

def calculate_technical_indicators(df):
    if len(df) < 50:
        return None
    df = df.copy()
    close = df["Close"]

    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()

    # 14-period RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=14, min_periods=14).mean()
    avg_loss = loss.rolling(window=14, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, 0.00001)
    df["rsi"] = 100 - (100 / (1 + rs))

    return df

def fetch_indicators_and_regime():
    market_data = {}
    tickers = [f"{s}.NS" for s in WATCHLIST.keys()]

    try:
        raw_df = yf.download(tickers, period="6mo", interval="1d", group_by="ticker", progress=False)
        for sym in WATCHLIST.keys():
            t_key = f"{sym}.NS"
            if t_key in raw_df and not raw_df[t_key].empty:
                df = raw_df[t_key].dropna()
                calc_df = calculate_technical_indicators(df)
                if calc_df is not None:
                    market_data[sym] = calc_df
    except Exception as e:
        print(f"⚠️ yfinance multi-download failed: {e}")

    # Evaluate Market Regime using NIFTYBEES
    regime = "NEUTRAL"
    if "NIFTYBEES" in market_data:
        nifty_df = market_data["NIFTYBEES"]
        last_close = safe_float(nifty_df["Close"].iloc[-1])
        last_ema50 = safe_float(nifty_df["ema50"].iloc[-1])
        regime = "AGGRESSIVE" if last_close > last_ema50 else "DEFENSIVE"

    print(f"Regime Sentinel: Market regime evaluated as '{regime}'")
    return market_data, regime

# ==============================================================================
# 8. AGENTS 2, 3, 4 & 5: AUTONOMOUS ALPHA, RISK, SIZING & MULTI-STAGE EXITS
# ==============================================================================

def execute_autonomous_entry(sym: str, close: float, cluster: str, memory: dict, trades: list) -> bool:
    """
    Autonomous Execution Agent: Sizes position, factors in STCL buffer, and records trade.
    """
    current_open = [t for t in trades if t.get("status") == "OPEN"]
    if len(current_open) >= MAX_ACTIVE_SLOTS:
        return False

    stcl_buffer = memory.get("stcl_pool", 0.0) / MAX_ACTIVE_SLOTS
    slot_capital = (INITIAL_CAPITAL / MAX_ACTIVE_SLOTS) + stcl_buffer
    qty = int(slot_capital // close)

    if qty <= 0:
        return False

    stop_loss = round(close * (1.0 - STOP_LOSS_PCT), 2)
    target_1 = round(close * (1.0 + BASE_TARGET_PCT), 2)
    current_date_str = datetime.now(IST).strftime("%Y-%m-%d")

    new_trade = {
        "symbol": sym,
        "entry_price": close,
        "units": qty,
        "remaining_units": qty,
        "sl": stop_loss,
        "entry_date": current_date_str,
        "status": "OPEN",
        "leg1_done": False,
        "exit_price": None,
        "exit_date": None,
        "exit_reason": None
    }
    trades.append(new_trade)
    save_json(DB_FILE, trades)

    msg = (
        f"🤖 *AUTONOMOUS AGENT EXECUTION: BOUGHT*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 *Asset:* `{sym}` ({cluster})\n"
        f"💰 *Entry Price:* ₹{close:.2f}\n"
        f"📦 *Position Size:* {qty} units (~₹{(qty * close):,.2f})\n"
        f"🛑 *Stop-Loss (Hard):* ₹{stop_loss:.2f} (-2.5%)\n"
        f"🎯 *Target 1 (50% scale):* ₹{target_1:.2f} (+3.5%)\n"
        f"🛡️ *Shield Applied:* ₹{stcl_buffer:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    send_telegram(msg)
    print(f"✅ Autonomous execution: Bought {qty} units of {sym} @ ₹{close:.2f}")
    return True

def manage_positions_and_scan(market_data: dict, regime: str, kite_ltps: dict):
    trades = load_json(DB_FILE, [])
    memory = load_json(MEMORY_FILE, {"stcl_pool": 0.0, "cooldowns": {}, "portfolio_peak": INITIAL_CAPITAL})

    active_trades = [t for t in trades if t.get("status") == "OPEN"]
    active_clusters = [WATCHLIST[t["symbol"]]["cluster"] for t in active_trades if t["symbol"] in WATCHLIST]

    # --- Agent 5: Position Management & Dynamic Multi-Stage Exits ---
    for t in active_trades:
        sym = t["symbol"]
        df = market_data.get(sym)
        entry_price = float(t["entry_price"])
        units = int(t["units"])
        rem_units = int(t.get("remaining_units", units))
        sl = float(t["sl"])
        target = round(entry_price * (1.0 + BASE_TARGET_PCT), 2)

        # Retrieve current price: Priority: Kite Live LTP -> Yahoo Close -> Entry Price Fallback
        current_price = kite_ltps.get(sym)
        if not current_price or current_price <= 0:
            if df is not None and not df.empty:
                current_price = safe_float(df["Close"].iloc[-1], default=entry_price)
            else:
                current_price = entry_price

        ema20 = entry_price
        if df is not None and not df.empty:
            ema20 = safe_float(df["ema20"].iloc[-1], default=entry_price)

        # Stage 1: Autonomous 50% Profit Realization (+3.5%) & Move SL to Breakeven
        if not t.get("leg1_done", False) and current_price >= target:
            half_qty = units // 2
            t["remaining_units"] = units - half_qty
            t["leg1_done"] = True
            t["sl"] = entry_price
            save_json(DB_FILE, trades)
            send_telegram(
                f"🎯 *AUTONOMOUS LEG 1 BOOKED: {sym}*\n"
                f"• Scaled Out: {half_qty} units @ ₹{current_price:.2f} (+3.5%)\n"
                f"• Runner: {t['remaining_units']} units trailing 20 EMA\n"
                f"• SL Adjusted to Breakeven: ₹{entry_price:.2f}"
            )

        # Stage 2 Runner: 20 EMA Trailing Exit
        elif t.get("leg1_done", False) and current_price < ema20:
            t["status"] = "CLOSED"
            t["exit_price"] = current_price
            t["exit_date"] = datetime.now(IST).strftime("%Y-%m-%d")
            t["exit_reason"] = "Leg 2 Trend Exit (20 EMA)"
            save_json(DB_FILE, trades)
            send_telegram(
                f"🏆 *AUTONOMOUS LEG 2 RUNNER CLOSED: {sym}*\n"
                f"• Exited Remaining: {rem_units} units @ ₹{current_price:.2f}\n"
                f"• Trigger: Price dropped below 20 EMA (₹{ema20:.2f})"
            )

        # Hard Stop-Loss (-2.5%)
        elif current_price <= sl:
            t["status"] = "CLOSED"
            t["exit_price"] = current_price
            t["exit_date"] = datetime.now(IST).strftime("%Y-%m-%d")
            t["exit_reason"] = "Stop-Loss Hit"
            loss_amount = (entry_price - current_price) * rem_units
            shield_add = loss_amount * STCL_SET_ASIDE_PCT
            memory["stcl_pool"] = memory.get("stcl_pool", 0.0) + shield_add
            save_json(DB_FILE, trades)
            save_json(MEMORY_FILE, memory)
            send_telegram(
                f"🛑 *AUTONOMOUS STOP-LOSS HIT: {sym}*\n"
                f"• Exited: {rem_units} units @ ₹{current_price:.2f}\n"
                f"• Realized Loss: -₹{loss_amount:.2f}\n"
                f"• Allocated to STCL Tax Shield: +₹{shield_add:.2f}"
            )

    # --- Agent 2: Opportunity Alpha Scanner ---
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

            # Quantitative Triple Confirmation Filter:
            # 1. Trend Alignment: Price > 50 EMA
            # 2. Value Zone: Price within 1.5% of 20 EMA
            # 3. Momentum Balance: 40 <= RSI <= 60
            if (close > ema50) and (abs(close - ema20) / close <= 0.015) and (40.0 <= rsi <= 60.0):
                executed = execute_autonomous_entry(sym, close, cluster, memory, trades)
                if executed:
                    available_slots -= 1
                    active_clusters.append(cluster)

# ==============================================================================
# 9. DASHBOARD BUILDER & TELEGRAM AUDIT SUMMARY
# ==============================================================================

def generate_dashboard_and_summary(market_data: dict, kite_ltps: dict):
    trades = load_json(DB_FILE, [])
    memory = load_json(MEMORY_FILE, {"stcl_pool": 0.0, "cooldowns": {}, "portfolio_peak": INITIAL_CAPITAL})

    open_trades = [t for t in trades if t.get("status") == "OPEN"]
    closed_trades = [t for t in trades if t.get("status") == "CLOSED"]

    total_realized_net = 0.0
    wins = 0

    for t in closed_trades:
        charges = calculate_statutory_charges(t["entry_price"], t["exit_price"], t["units"])
        net = charges["net_pnl"]
        total_realized_net += net
        if net > 0:
            wins += 1

    win_rate = (wins / len(closed_trades) * 100) if closed_trades else 0.0
    current_time_str = datetime.now(IST).strftime("%H:%M:%S IST")

    # Generate Telegram Status Report
    pos_lines = []
    if not open_trades:
        pos_lines.append("• Active Slots: 0/3 (100% Cash)")
    else:
        for t in open_trades:
            sym = t["symbol"]
            entry = float(t["entry_price"])
            units = int(t.get("remaining_units", t["units"]))
            ltp = kite_ltps.get(sym, safe_float(market_data[sym]["Close"].iloc[-1], default=entry) if sym in market_data else entry)
            if ltp <= 0:
                ltp = entry

            charges = calculate_statutory_charges(entry, ltp, units)
            gross = charges["gross_pnl"]
            net = charges["net_pnl"]
            est_fees = charges["total_charges"]
            pct = (net / (entry * units)) * 100 if entry > 0 else 0.0

            pos_lines.append(
                f"• `{sym}`: {units} units\n"
                f" Entry: ₹{entry:.2f} | LTP: ₹{ltp:.2f}\n"
                f" 🎯 Target: ₹{entry * (1.0 + BASE_TARGET_PCT):.2f} | 🛑 SL: ₹{t['sl']:.2f}\n"
                f" Net P&L: ₹{net:,.2f} ({pct:+.2f}%)\n"
                f" (Gross: ₹{gross:,.2f} | Est. Fees: ₹{est_fees:.2f})"
            )

    summary_msg = (
        "📊 *SWING ENGINE STATUS*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🟢 *Status:* Scan Complete\n"
        f"⏰ *Server Time:* `{current_time_str}`\n"
        f"💰 *Capital Base:* ₹{INITIAL_CAPITAL:,.2f}\n"
        f"🛡️ *Tax Shield:* ₹{memory.get('stcl_pool', 0.0):,.2f}\n\n"
        f"*Open Positions ({len(open_trades)}/{MAX_ACTIVE_SLOTS}):*\n"
        + "\n\n".join(pos_lines) + "\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    send_telegram(summary_msg)

    # Generate Live HTML Dashboard
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Autonomous ETF Swing Engine</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body {{ background-color: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
        .card {{ background-color: #161b22; border: 1px solid #30363d; border-radius: 8px; }}
        .table {{ color: #c9d1d9; }}
        .table-dark {{ background-color: #161b22; }}
        .positive {{ color: #3fb950; font-weight: 600; }}
        .negative {{ color: #f85149; font-weight: 600; }}
    </style>
</head>
<body class="py-4">
    <div class="container">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <h2 class="h4 mb-0">Autonomous Index & ETF Swing Dashboard</h2>
            <span class="badge bg-secondary">Updated: {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}</span>
        </div>

        <div class="row g-3 mb-4">
            <div class="col-md-3">
                <div class="card p-3">
                    <div class="text-muted small">Capital Base</div>
                    <div class="h5 mb-0">₹{INITIAL_CAPITAL:,.2f}</div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card p-3">
                    <div class="text-muted small">STCL Tax Shield</div>
                    <div class="h5 mb-0 text-info">₹{memory.get('stcl_pool', 0.0):,.2f}</div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card p-3">
                    <div class="text-muted small">Realized Net P&L</div>
                    <div class="h5 mb-0 {'positive' if total_realized_net >= 0 else 'negative'}">₹{total_realized_net:,.2f}</div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card p-3">
                    <div class="text-muted small">Win Rate</div>
                    <div class="h5 mb-0">{win_rate:.1f}% ({wins}/{len(closed_trades)})</div>
                </div>
            </div>
        </div>

        <div class="card p-4 mb-4">
            <h5 class="card-title h6 mb-3">Active Positions ({len(open_trades)}/{MAX_ACTIVE_SLOTS})</h5>
            <div class="table-responsive">
                <table class="table table-dark table-hover mb-0">
                    <thead>
                        <tr>
                            <th>Symbol</th>
                            <th>Entry Date</th>
                            <th>Units</th>
                            <th>Entry</th>
                            <th>LTP</th>
                            <th>SL</th>
                            <th>Target</th>
                            <th>Net P&L</th>
                        </tr>
                    </thead>
                    <tbody>
    """

    for t in open_trades:
        sym = t["symbol"]
        entry = float(t["entry_price"])
        rem = int(t.get("remaining_units", t["units"]))
        ltp = kite_ltps.get(sym, safe_float(market_data[sym]["Close"].iloc[-1], default=entry) if sym in market_data else entry)
        if ltp <= 0:
            ltp = entry
        charges = calculate_statutory_charges(entry, ltp, rem)
        net = charges["net_pnl"]

        html_content += f"""
                        <tr>
                            <td><strong>{sym}</strong></td>
                            <td>{t['entry_date']}</td>
                            <td>{rem}</td>
                            <td>₹{entry:.2f}</td>
                            <td>₹{ltp:.2f}</td>
                            <td>₹{t['sl']:.2f}</td>
                            <td>₹{entry * (1.0 + BASE_TARGET_PCT):.2f}</td>
                            <td class="{'positive' if net >= 0 else 'negative'}">₹{net:,.2f}</td>
                        </tr>
        """

    html_content += """
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</body>
</html>
    """
    os.makedirs(os.path.dirname(DASHBOARD_FILE), exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"✅ Dashboard generated at: {DASHBOARD_FILE}")

# ==============================================================================
# 10. ENGINE ORCHESTRATOR & ENTRY POINT
# ==============================================================================

def run_trading_engine():
    now_ist = datetime.now(IST)
    print(f"\n--- Running Swing Engine Cycle: {now_ist.strftime('%Y-%m-%d %H:%M:%S IST')} ---")

    # Dynamic Sentinel: Early exit on weekends and holidays
    if not is_market_open():
        print(f"Status: Market is closed ({now_ist.strftime('%A, %H:%M IST')}). Skipping execution.")
        return

    # 1. Establish Kite session
    kite = initialize_kite_session()

    # 2. Technical analysis data extraction
    market_data, regime = fetch_indicators_and_regime()

    # 3. Pull real-time quotes if Kite is connected
    kite_ltps = fetch_kite_live_ltp(kite, list(WATCHLIST.keys())) if kite else {}

    # 4. Autonomous scans, risk evaluations, entries, and multi-stage exits
    manage_positions_and_scan(market_data, regime, kite_ltps)

    # 5. Compile live dashboard and dispatch Telegram audit status
    generate_dashboard_and_summary(market_data, kite_ltps)

if __name__ == "__main__":
    # Process any incoming /status queries
    process_telegram_updates()
    
    # Execute full trading cycle
    run_trading_engine()
