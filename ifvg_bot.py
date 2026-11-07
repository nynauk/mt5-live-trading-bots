import MetaTrader5 as mt5
import time
import hashlib
import numpy as np
from datetime import datetime, timedelta

# === Configuration ===
SYMBOLS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD",
    "USDCAD", "EURGBP", "EURJPY", "GBPJPY"
]
TIMEFRAME = mt5.TIMEFRAME_M5
MAX_TRACK_CANDLES = 7

# === Session Ranges (Server Time UTC+3) ===
ASIA_START, ASIA_END = 3, 9           # 01:00–07:00 BST
LONDON_START, LONDON_END = 9, 12      # 07:00–10:15 BST
LDN_NY_XOVER_START, LDN_NY_XOVER_END = 12, 15  # 10:15–13:00 BST
NY_AM_START, NY_AM_END = 15, 17       # 13:00–15:15 BST
NY_PM_START, NY_PM_END = 21, 23       # 19:30–21:30 BST

# === State Tracking ===
fvg_memory = {symbol: [] for symbol in SYMBOLS}
session_ranges = {
    symbol: {
        "asia": {"high": None, "low": None},
        "london": {"high": None, "low": None}
    } for symbol in SYMBOLS
}
sweeps = {symbol: {"london": None, "newyork": None} for symbol in SYMBOLS}
killzone_trades = {symbol: {"london": False, "newyork": False} for symbol in SYMBOLS}
last_reset_day = None

# === Logging ===
def log(msg):
    print(f"[{datetime.now()}] {msg}")

def calculate_position_size(symbol, entry_price, sl_price):
    account = mt5.account_info()
    info = mt5.symbol_info(symbol)
    if not account or not info:
        return None
    risk_amount = account.balance * 0.001
    stop_loss_distance = abs(entry_price - sl_price)
    tick_value = info.trade_tick_value
    tick_size = info.trade_tick_size
    if tick_size == 0 or tick_value == 0:
        return None
    stop_loss_in_ticks = stop_loss_distance / tick_size
    cost_per_lot = stop_loss_in_ticks * tick_value
    if cost_per_lot == 0:
        return None
    lots = risk_amount / cost_per_lot
    lots = max(info.volume_min, min(lots, info.volume_max))
    lots = round(lots / info.volume_step) * info.volume_step
    return lots

def mark_session_high_low(symbol, start_hour, end_hour, session_key):
    now = datetime.now()
    from_time = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    to_time = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    rates = mt5.copy_rates_range(symbol, TIMEFRAME, from_time, to_time)
    if rates is None or len(rates) == 0:
        return
    highs = [r['high'] for r in rates]
    lows = [r['low'] for r in rates]
    high_val = max(highs)
    low_val = min(lows)
    session_ranges[symbol][session_key]['high'] = high_val
    session_ranges[symbol][session_key]['low'] = low_val
    log(f"{symbol} | {session_key.upper()} Session ({datetime.now().date()}) High: {high_val}, Low: {low_val}")

def detect_sweep(symbol, session_key, candle):
    high = session_ranges[symbol][session_key]["high"]
    low = session_ranges[symbol][session_key]["low"]
    if high is None or low is None:
        return None
    if candle['high'] > high:
        return "high"
    elif candle['low'] < low:
        return "low"
    return None

def detect_fvg(c0, c2):
    if c0['low'] > c2['high']:  # Bullish FVG
        return {"type": "bullish", "high": c0['low'], "low": c2['high']}
    elif c0['high'] < c2['low']:  # Bearish FVG
        return {"type": "bearish", "high": c2['low'], "low": c0['high']}
    return None

def execute_trade(symbol, direction, entry, sl, tp, killzone):
    lots = calculate_position_size(symbol, entry, sl)
    if lots is None or lots <= 0:
        log(f"{symbol} | Invalid lot size")
        return
    tick = mt5.symbol_info_tick(symbol)
    price = tick.ask if direction == "buy" else tick.bid
    order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lots,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 25,
        "magic": 10001,
        "comment": f"IFVG {direction.upper()}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"{symbol} | Order failed: {result.retcode} - {result.comment}")
    else:
        killzone_trades[symbol][killzone] = True
        log(f"{symbol} | {direction.upper()} TRADE | Entry={entry} SL={sl} TP={tp} Lots={lots}")

def strategy(symbol):
    global last_reset_day
    now = datetime.now()

    # Reset daily
    if last_reset_day != now.date():
        for s in SYMBOLS:
            sweeps[s] = {"london": None, "newyork": None}
            killzone_trades[s] = {"london": False, "newyork": False}
        last_reset_day = now.date()
        log("🔄 Daily state reset")
        for s in SYMBOLS:
            for session in ["asia", "london"]:
                high = session_ranges[s][session]["high"]
                low = session_ranges[s][session]["low"]
            if high and low:
                log(f"{s} | {session.upper()} Session ({datetime.now().date()}) Final Range -> High: {high}, Low: {low}")

    # Mark session highs/lows
    if now.hour >= ASIA_END and session_ranges[symbol]["asia"]["high"] is None:
        mark_session_high_low(symbol, ASIA_START, ASIA_END, "asia")
    if now.hour >= LONDON_END and session_ranges[symbol]["london"]["high"] is None:
        mark_session_high_low(symbol, LONDON_START, LONDON_END, "london")

    # Get candles
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, 6)
    if rates is None or len(rates) < 6:
        return

    c0, c2 = rates[-5], rates[-3]
    last_candle = rates[-2]
    close_price = last_candle['close']
    fvg = detect_fvg(c0, c2)
    if not fvg:
        return

    current_hour = now.hour
    killzone = None
    session_key = None

    if LONDON_START <= current_hour < LONDON_END or (current_hour == LONDON_END and now.minute <= 15):
        killzone = "london"
        session_key = "asia"
    elif LDN_NY_XOVER_START <= current_hour < NY_AM_END:  # LDN/NY crossover included
        killzone = "newyork"
        session_key = "london"

    if not killzone or killzone_trades[symbol][killzone]:
        return

    sweep = detect_sweep(symbol, session_key, last_candle)
    if not sweep:
        return

    sweeps[symbol][killzone] = sweep

    # Trade logic based on sweep + FVG disrespect
    if sweep == "high" and fvg["type"] == "bullish" and close_price < fvg["low"]:
        sl = max(fvg['high'], last_candle['high'])
        tp = close_price - 2 * abs(close_price - sl)
        execute_trade(symbol, "sell", close_price, sl, tp, killzone)

    elif sweep == "low" and fvg["type"] == "bearish" and close_price > fvg["high"]:
        sl = min(fvg['low'], last_candle['low'])
        tp = close_price + 2 * abs(close_price - sl)
        execute_trade(symbol, "buy", close_price, sl, tp, killzone)

def main():
    if not mt5.initialize():
        log("❌ MetaTrader5 initialization failed")
        return
    log("✅ IFVG Asia & London/NY Sweep Strategy Started")
    while True:
        for symbol in SYMBOLS:
            try:
                if not mt5.symbol_select(symbol, True):
                    log(f"{symbol} | Could not select.")
                    continue
                strategy(symbol)
            except Exception as e:
                log(f"{symbol} | ERROR: {e}")
        time.sleep(10)

main()
