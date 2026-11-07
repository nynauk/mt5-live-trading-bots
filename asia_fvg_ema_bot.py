import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta
import traceback

RR_RATIO = 3.0
RISK_PERCENT = 0.1

ASIAN_START = 4
ASIAN_END = 10
TRADE_WINDOW_START = 10
TRADE_WINDOW_END = 13

EMA_TIMEFRAME = mt5.TIMEFRAME_H4
EMA_PERIOD = 50
FVG_TIMEFRAME = mt5.TIMEFRAME_M3

SYMBOLS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
    "AUDUSD", "NZDUSD",
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY",
    "CADJPY", "CHFJPY", "NZDJPY", "EURCHF",
    "EURCAD", "EURNZD", "GBPCAD", "GBPAUD",
    "GBPNZD", "AUDCAD", "AUDNZD", "CADCHF",
    "NZDCAD",
    "US30.cash", "US100.cash", "US500.cash",
    "US2000.cash", "JP225.cash", "GER40.cash",
    "UK100.cash", "AUS200.cash", "HK50.cash",
    "XAUUSD", "XAGUSD", "XPDUSD", "XPTUSD",
    "USOIL.cash", "DXY.cash"
]

asian_ranges = {}
daily_traded_symbols = {}

def get_server_time():
    symbol = "EURUSD"
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return datetime.now()
    return datetime.fromtimestamp(tick.time)

def log(msg):
    try:
        server_now = get_server_time()
    except:
        server_now = datetime.now()
    print(f"[{server_now}] {msg}")

def get_asian_range(symbol):
    server_now = get_server_time()
    today = server_now.date()
    start_time = datetime(today.year, today.month, today.day, ASIAN_START, 0)
    end_time = datetime(today.year, today.month, today.day, ASIAN_END, 0)
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M15, start_time, end_time)
    if rates is None or len(rates) == 0:
        return None
    highs = [r['high'] for r in rates]
    lows = [r['low'] for r in rates]
    return max(highs), min(lows)

def get_ema_filter(symbol):
    rates = mt5.copy_rates_from_pos(symbol, EMA_TIMEFRAME, 0, EMA_PERIOD + 1)
    if rates is None or len(rates) < EMA_PERIOD:
        return None
    closes = [r['close'] for r in rates]
    ema50 = pd.Series(closes).ewm(span=EMA_PERIOD).mean().iloc[-1]
    price = closes[-1]
    return ema50, price

def detect_structure_shift(symbol, direction, sweep_level):
    rates = mt5.copy_rates_from_pos(symbol, FVG_TIMEFRAME, 0, 100)
    if rates is None or len(rates) < 10:
        return None, None

    for i in range(10, len(rates)):
        swing_highs = max([bar['high'] for bar in rates[i-10:i]])
        swing_lows = min([bar['low'] for bar in rates[i-10:i]])
        close = rates[i]['close']

        if direction == "buy" and close > swing_highs and sweep_level > swing_lows:
            return rates[i:], i
        if direction == "sell" and close < swing_lows and sweep_level < swing_highs:
            return rates[i:], i

    return None, None

def detect_fvgs(rates, direction):
    fvgs = []
    for i in range(2, len(rates)):
        c0 = rates[i-2]
        c2 = rates[i]

        if direction == "buy" and c0['high'] < c2['low']:
            fvgs.append((c0['high'], c2['low'], i))

        elif direction == "sell" and c0['low'] > c2['high']:
            fvgs.append((c2['high'], c0['low'], i))

    return fvgs

def is_unmitigated(fvg, rates, direction):
    fvg_low, fvg_high, index = fvg

    if direction == "buy":
        future_bars = rates[index+1:]
        for bar in future_bars:
            if bar['low'] <= fvg_low:
                return False
        return True

    if direction == "sell":
        future_bars = rates[index+1:]
        for bar in future_bars:
            if bar['high'] >= fvg_high:
                return False
        return True

    return False

def select_best_unmitigated_fvg(fvgs, rates, direction):
    candidates = []
    for fvg in fvgs:
        if is_unmitigated(fvg, rates, direction):
            candidates.append(fvg)

    if not candidates:
        return None

    if direction == "buy":
        return min(candidates, key=lambda x: x[0])
    if direction == "sell":
        return max(candidates, key=lambda x: x[1])

    return None

def calculate_position_size(symbol, entry_price, stop_loss_price):
    account = mt5.account_info()
    symbol_info = mt5.symbol_info(symbol)
    if not account or not symbol_info:
        log(f"Error: Could not get account or symbol info for {symbol}")
        return None

    risk_amount = account.balance * (RISK_PERCENT / 100)
    stop_loss_distance = abs(entry_price - stop_loss_price)
    tick_value = symbol_info.trade_tick_value
    tick_size = symbol_info.trade_tick_size

    if tick_size == 0 or tick_value == 0:
        log(f"Error: Tick size or tick value zero for {symbol}")
        return None

    stop_loss_in_ticks = stop_loss_distance / tick_size
    cost_per_lot = stop_loss_in_ticks * tick_value
    lots = risk_amount / cost_per_lot

    lots = max(symbol_info.volume_min, min(lots, symbol_info.volume_max))
    lots = round(lots / symbol_info.volume_step) * symbol_info.volume_step
    return lots

def place_limit_order(symbol, order_type, entry_price, sl_price, tp_price):
    symbol_info = mt5.symbol_info(symbol)
    lots = calculate_position_size(symbol, entry_price, sl_price)
    if lots is None:
        log(f"Skipping {symbol} due to invalid lot calculation.")
        return

    request = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": lots,
        "type": order_type,
        "price": entry_price,
        "sl": sl_price,
        "tp": tp_price,
        "deviation": 25,
        "magic": 123456,
        "comment": "SMC FVG Entry",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"LIMIT ORDER FAILED for {symbol}: retcode={result.retcode}, message={mt5.last_error()}")
    else:
        log(f"LIMIT ORDER SUCCESS for {symbol}: ticket={result.order}, volume={lots}")
        daily_traded_symbols[symbol] = True

def check_and_trade(symbol):
    global asian_ranges, daily_traded_symbols
    server_now = get_server_time()
    today = server_now.date()

    if symbol not in asian_ranges or asian_ranges[symbol]['date'] != today:
        asian_range = get_asian_range(symbol)
        if not asian_range:
            log(f"Could not retrieve Asian range for {symbol}")
            return
        asian_ranges[symbol] = {'high': asian_range[0], 'low': asian_range[1], 'date': today}
        daily_traded_symbols[symbol] = False
        log(f"{symbol}: Asian Range set. High: {asian_range[0]}, Low: {asian_range[1]}")

    if daily_traded_symbols.get(symbol, False):
        return

    high = asian_ranges[symbol]['high']
    low = asian_ranges[symbol]['low']

    if not (TRADE_WINDOW_START <= server_now.hour < TRADE_WINDOW_END):
        return

    ema_data = get_ema_filter(symbol)
    if not ema_data:
        log(f"Could not calculate EMA for {symbol}")
        return

    ema50, price = ema_data
    tick = mt5.symbol_info_tick(symbol)
    current_price = tick.last

    positions = mt5.positions_get(symbol=symbol)
    if positions and len(positions) > 0:
        return

    # Sell Setup
    if current_price > high and price < ema50:
        rates_after, _ = detect_structure_shift(symbol, "sell", high)
        if not rates_after:
            log(f"{symbol}: No bearish structure shift detected")
            return

        fvgs = detect_fvgs(rates_after, "sell")
        best_fvg = select_best_unmitigated_fvg(fvgs, rates_after, "sell")
        if not best_fvg:
            log(f"{symbol}: No valid unmitigated bearish FVG after MSS")
            return

        fvg_low, fvg_high, _ = best_fvg
        entry = (fvg_low + fvg_high) / 2
        sl = high
        risk = entry - sl
        tp = entry - (risk * RR_RATIO)
        log(f"{symbol}: SELL LIMIT at {entry}")
        place_limit_order(symbol, mt5.ORDER_TYPE_SELL_LIMIT, entry, sl, tp)

    # Buy Setup
    elif current_price < low and price > ema50:
        rates_after, _ = detect_structure_shift(symbol, "buy", low)
        if not rates_after:
            log(f"{symbol}: No bullish structure shift detected")
            return

        fvgs = detect_fvgs(rates_after, "buy")
        best_fvg = select_best_unmitigated_fvg(fvgs, rates_after, "buy")
        if not best_fvg:
            log(f"{symbol}: No valid unmitigated bullish FVG after MSS")
            return

        fvg_low, fvg_high, _ = best_fvg
        entry = (fvg_low + fvg_high) / 2
        sl = low
        risk = sl - entry
        tp = entry + (abs(risk) * RR_RATIO)
        log(f"{symbol}: BUY LIMIT at {entry}")
        place_limit_order(symbol, mt5.ORDER_TYPE_BUY_LIMIT, entry, sl, tp)

while True:
    try:
        if not mt5.initialize():
            log("MT5 not initialized, attempting reconnect...")
            time.sleep(10)
            continue

        for symbol in SYMBOLS:
            try:
                if not mt5.symbol_select(symbol, True):
                    log(f"Failed to select symbol {symbol}")
                    continue
                check_and_trade(symbol)
            except Exception as e:
                log(f"Error in symbol loop for {symbol}: {str(e)}")
                log(traceback.format_exc())

        time.sleep(30)

    except Exception as e:
        log(f"MAIN LOOP ERROR: {str(e)}")
        log(traceback.format_exc())
        time.sleep(10)
