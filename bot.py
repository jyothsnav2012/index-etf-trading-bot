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
# 2. NOTIFICATIONS & FILE UTILITIES
# =====================================================================
def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"Notification (Telegram credentials missing):\n{message}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
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
# 3. AUTHENTICATION (AUTOMATED TOTP)
# =====================================================================
def get_kite_session():
    api_key = os.getenv("KITE_API_KEY")
    api_secret = os.getenv("KITE_API_SECRET")
    user_id = os.getenv("KITE_USER_ID")
    password = os.getenv("KITE_PASSWORD")
    totp_secret = os.getenv("KITE_TOTP_SECRET")

    if not all([api_key, api_secret, user_id, password, totp_secret]):
        return None

    try:
        kite = KiteConnect(api_key=api_key)
        totp = pyotp.TOTP(totp_secret).now()
        session = requests.Session()

        login_res = session.post(
            "https://kite.zerodha.com/api/login",
            data={"user_id": user_id, "password": password},
            timeout=10,
        ).json()
        request_id = login_res["data"]["request_id"]

        twofa_res = session.post(
            "https://kite.zerodha.com/api/twofa",
            data={
                "user_id": user_id,
                "request_id": request_id,
                "twofa_value": totp,
                "skip_session": "",
            },
            timeout=10,
        ).json()

        req_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&skip_session=true"
        resp = session.get(req_url, timeout=10)
        request_token = resp.url.split("request_token=")[1].split("&")[0]

        data = kite.generate_session(request_token, api_secret=api_secret)
        kite.set_access_token(data["access_token"])
        return kite
    except Exception as e:
        print(f"Kite Connect Authentication Error: {e}")
        return None


# =====================================================================
# 4. NSE HOLIDAY & TIMING GUARD
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


# =====================================================================
# 5. TAX SHIELD & STATUTORY CHARGES CALCULATION
# =====================================================================
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
# 6. RELATIVE STRENGTH & RESEARCH BUY AGENT
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
                send_telegram(msg)
                return


# =====================================================================
# 7. EXIT & POSITION MONITORING AGENT
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

        # Opening Gap-Up Harvest (09:15 - 09:30 AM)
        if (
            time(9, 15) <= current_time <= time(9, 30)
            and pnl_pct >= 4.0
            and not trade.get("leg1_done")
        ):
            exit_type = "Opening Gap-Up Profit Harvest"
            sell_units = units // 2
            trade["leg1_done"] = True

        # Standard Leg 1 Target Hit (+3.5%)
        elif (
            current_price >= round(entry_price * (1 + BASE_TARGET_PCT), 2)
            and not trade.get("leg1_done")
        ):
            exit_type = "Leg 1 Target Achieved (+3.5%)"
            sell_units = units // 2
            trade["leg1_done"] = True
            trade["sl"] = entry_price

        # Leg 2 Runner Exit
        elif trade.get("leg1_done"):
            if current_price < ema10_val or current_price <= trade["sl"]:
                exit_type = f"Leg 2 Runner Exit (P&L: {pnl_pct:+.2f}%)"
                sell_units = trade.get("remaining_units", units - (units // 2))
                trade["status"] = "CLOSED"

        # Stop-Loss Hit (-2.5%)
        elif current_price <= sl:
            exit_type = "Stop-Loss Hit (-2.5%)"
            sell_units = units
            trade["status"] = "CLOSED"

        # 30-Day Timeout
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
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"*Justification:*\n"
                f"• Strict adherence to risk/reward boundaries.\n"
                f"• Unlocked capital returned to investable cash pool."
            )
            send_telegram(msg)

        updated_trades.append(trade)

    save_json(DB_FILE, updated_trades)


# =====================================================================
# 8. REFLECTION AGENT & MAIN EXECUTION
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
                    f"• Placed on 5-day quarantine until {cooldown_until} following repeated stop-outs."
                )

    save_json(MEMORY_FILE, memory)


if __name__ == "__main__":
    is_open, msg = is_market_open()
    print(f"Status: {msg}")

    # Fallback to simulated data if Kite credentials are not yet configured
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
            # Fallback simulator for offline testing
            market_data[sym] = pd.DataFrame(
                {
                    "close": [250.0 + i * 0.5 for i in range(50)],
                    "volume": [10000] * 50,
                }
            )

    evaluate_positions_and_exits(market_data)
    scan_for_buy_entries(market_data, INITIAL_CAPITAL)

    if datetime.now().time() >= time(15, 30):
        run_post_market_reflection()
