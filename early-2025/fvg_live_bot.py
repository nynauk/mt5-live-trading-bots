import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import datetime
import time

# -------------------------------
# 1. INITIALIZE MT5 & GLOBAL SETTINGS
# -------------------------------
if not mt5.initialize():
    print("MT5 initialization failed.")
    mt5.shutdown()
    exit()

# Define forex pairs and the analysis timeframe (change to mt5.TIMEFRAME_M5 if desired)
forex_pairs = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "USDCHF"]
analysis_timeframe = mt5.TIMEFRAME_M3

# Account & risk parameters.
account_size = 100000  # Account size in USD
risk_percent = 0.01  # 1% risk per trade
risk_amount = account_size * risk_percent  # $1,000 risk per trade

# Maximum allowed volume (in standard lots) per trade (adjust as needed)
max_lot_allowed = 1.0

# Global dictionary to track if a setup has already been triggered for each symbol today.
active_setups = {}


# -------------------------------
# 2. HELPER FUNCTIONS
# -------------------------------

def get_server_time():
    """
    Retrieve the current server time from MetaTrader5 using the EURUSD tick.
    This time (assumed to be the broker’s server time) is used for all time checks.
    """
    tick = mt5.symbol_info_tick("EURUSD")
    if tick is None:
        print("DEBUG: Could not get server time from EURUSD tick; using system UTC time.")
        return datetime.datetime.utcnow()
    return datetime.datetime.fromtimestamp(tick.time)


def calculate_atr(df, period=14):
    """Calculate ATR from OHLC data in a DataFrame."""
    df['prev_close'] = df['close'].shift(1)
    df['tr'] = df.apply(lambda row: max(row['high'] - row['low'],
                                        abs(row['high'] - row['prev_close']),
                                        abs(row['low'] - row['prev_close'])),
                        axis=1)
    df['atr'] = df['tr'].rolling(window=period).mean()
    return df


def get_atr_live(symbol, period=14):
    """
    Fetch recent bars for the symbol on the chosen timeframe and compute the ATR.
    (This function is available for further risk management if desired.)
    """
    bars = mt5.copy_rates_from_pos(symbol, analysis_timeframe, 0, period + 5)
    if bars is None or len(bars) < period:
        print(f"DEBUG: Not enough bars for ATR for {symbol}")
        return None
    df = pd.DataFrame(bars)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df.set_index('time', inplace=True)
    df = calculate_atr(df, period=period)
    return df['atr'].iloc[-1]


def get_pip_info(symbol):
    """
    Retrieve pip size and pip value (per standard lot) for a symbol.
    For most USD‑quoted pairs, pip_size is 0.0001 (or 0.01 for JPY pairs)
    and pip_value is approximately $10 per standard lot.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        print(f"DEBUG: Symbol info not found for {symbol}")
        return None, None
    pip_size = info.point * 10  # e.g., 0.0001 for most pairs; 0.01 for JPY pairs
    if symbol.endswith("JPY"):
        pip_value = (pip_size * 100000) / info.ask
    else:
        pip_value = 10
    return pip_size, pip_value


def detect_coch_mss_fvg(symbol, timeframe):
    """
    Detect a Change of Character (CoCH) or Market Structure Shift (MSS) that leaves
    behind a Fair Value Gap (FVG) using a simple three‐candle pattern.

    Returns a dictionary with details if a setup is detected:
      For bullish setups (enter with a BUY):
         { "setup": "bullish", "fvg_low": <float>, "fvg_high": <float>,
           "mid_fvg": <float>, "stop_loss": <float> }
      For bearish setups (enter with a SELL):
         { "setup": "bearish", "fvg_low": <float>, "fvg_high": <float>,
           "mid_fvg": <float>, "stop_loss": <float> }
    Otherwise, returns None.

    Demo logic: Use the last 3 candles.
      - Bullish: if the most recent candle closes above the previous candle’s high.
      - Bearish: if the most recent candle closes below the previous candle’s low.
      - An FVG is considered to exist if there’s a gap between candle A and candle C.
    """
    bars = mt5.copy_rates_from_pos(symbol, timeframe, 0, 5)
    if bars is None or len(bars) < 3:
        return None
    df = pd.DataFrame(bars)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df.set_index('time', inplace=True)
    recent = df.iloc[-3:]
    candle_A = recent.iloc[0]  # oldest
    candle_B = recent.iloc[1]
    candle_C = recent.iloc[2]  # most recent

    # Bullish setup:
    if candle_C['close'] > candle_B['high']:
        fvg_low = candle_C['low']
        fvg_high = candle_A['high']
        gap = fvg_low - fvg_high
        if gap > 0:
            mid_fvg = fvg_high + gap / 2
            swing_low = min(candle_A['low'], candle_B['low'], candle_C['low'])
            return {
                "setup": "bullish",
                "fvg_low": fvg_high,
                "fvg_high": fvg_low,
                "mid_fvg": mid_fvg,
                "stop_loss": swing_low
            }
    # Bearish setup:
    if candle_C['close'] < candle_B['low']:
        fvg_low = candle_A['low']
        fvg_high = candle_C['high']
        gap = fvg_high - fvg_low
        if gap > 0:
            mid_fvg = fvg_low + gap / 2
            swing_high = max(candle_A['high'], candle_B['high'], candle_C['high'])
            return {
                "setup": "bearish",
                "fvg_low": fvg_low,
                "fvg_high": fvg_high,
                "mid_fvg": mid_fvg,
                "stop_loss": swing_high
            }
    return None


def adjust_volume(volume, symbol_info):
    """
    Adjust the given volume so that it is within the broker's allowed range and is a multiple of volume_step.
    """
    min_vol = symbol_info.volume_min
    max_vol = symbol_info.volume_max
    step = symbol_info.volume_step
    if volume < min_vol:
        volume = min_vol
    if volume > max_vol:
        volume = max_vol
    volume = round(volume / step) * step
    return volume


def execute_fvg_market_order(symbol, order_type, stop_loss, lot_size):
    """
    Place an immediate market order for the symbol.
    For a BUY, we use ORDER_TYPE_BUY (executed at the current Ask);
    for a SELL, we use ORDER_TYPE_SELL (executed at the current Bid).
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        print(f"DEBUG: Symbol info not found for {symbol}")
        return None
    if not info.visible:
        mt5.symbol_select(symbol, True)
    # Adjust volume per broker restrictions.
    lot_size = adjust_volume(lot_size, info)
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        print(f"DEBUG: Could not get tick info for {symbol}")
        return None
    if order_type == "buy":
        mt5_order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
    else:
        mt5_order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
    deviation = 10
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot_size,
        "type": mt5_order_type,
        "price": price,
        "sl": stop_loss,
        "tp": 0,  # No take profit defined here
        "deviation": deviation,
        "magic": 123456,
        "comment": "FVG CoCH/MSS Strategy Market Order",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN,
    }
    print(f"DEBUG: Market Order Request for {symbol}: {request}")
    print(f"DEBUG: Current tick for {symbol}: Bid={tick.bid}, Ask={tick.ask}")
    result = mt5.order_send(request)
    if result is None:
        print(f"DEBUG: order_send returned None for {symbol} in market order.")
        return None
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"DEBUG: Market order for {symbol} failed: retcode={result.retcode}, comment={result.comment}")
        return None
    print(
        f"DEBUG: Market {order_type.upper()} order for {symbol} executed at price {price} (SL: {stop_loss}, Lot: {lot_size})")
    return result.order


# -------------------------------
# 3. MAIN LIVE TRADING LOOP
# -------------------------------
print("Starting live FVG CoCH/MSS strategy bot (Market Orders)...")

while True:
    server_time = get_server_time()
    current_date = server_time.date()

    for symbol in forex_pairs:
        # Only one setup per symbol per day.
        if symbol in active_setups:
            continue

        # Detect a CoCH/MSS with an FVG on the chosen timeframe.
        setup = detect_coch_mss_fvg(symbol, analysis_timeframe)
        if setup is None:
            continue

        # Determine order direction.
        order_direction = "buy" if setup["setup"] == "bullish" else "sell"
        entry_price = round(setup["mid_fvg"], 5)
        stop_loss = round(setup["stop_loss"], 5)

        # Calculate risk in pips and the calculated lot size.
        pip_size, pip_value = get_pip_info(symbol)
        if pip_size is None or pip_value is None:
            continue

        risk_pips = abs(entry_price - stop_loss) / pip_size
        if risk_pips == 0:
            print(f"DEBUG: Risk pips is zero for {symbol}. Skipping setup.")
            continue

        lot_size = risk_amount / (risk_pips * pip_value)
        lot_size = round(lot_size, 2)

        # Cap the calculated lot size to a maximum allowed volume.
        if lot_size > max_lot_allowed:
            print(
                f"DEBUG: Calculated lot size {lot_size} for {symbol} exceeds max allowed {max_lot_allowed}. Capping to {max_lot_allowed}.")
            lot_size = max_lot_allowed

        print(f"DEBUG: {symbol} setup detected at {server_time} - {setup['setup'].upper()} setup")
        print(f"       FVG zone: {round(setup['fvg_low'], 5)} - {round(setup['fvg_high'], 5)}, Mid: {entry_price}")
        print(f"       Stop loss set at: {stop_loss}, Risk (pips): {risk_pips:.2f}, Calculated Lot size: {lot_size}")

        # Get the current tick.
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            print(f"DEBUG: Could not get tick for {symbol}")
            continue

        # Check if the market has reached the mid-FVG level:
        if order_direction == "buy":
            if tick.ask >= entry_price:
                print(
                    f"DEBUG: {symbol} BUY condition met: Current Ask {tick.ask} >= Entry {entry_price}. Executing market order.")
                ticket = execute_fvg_market_order(symbol, order_direction, stop_loss, lot_size)
                if ticket:
                    active_setups[symbol] = ticket
            else:
                print(f"DEBUG: {symbol} BUY condition not met: Current Ask {tick.ask} < Entry {entry_price}.")
        else:
            if tick.bid <= entry_price:
                print(
                    f"DEBUG: {symbol} SELL condition met: Current Bid {tick.bid} <= Entry {entry_price}. Executing market order.")
                ticket = execute_fvg_market_order(symbol, order_direction, stop_loss, lot_size)
                if ticket:
                    active_setups[symbol] = ticket
            else:
                print(f"DEBUG: {symbol} SELL condition not met: Current Bid {tick.bid} > Entry {entry_price}.")

    # Optional: Reset setups at the start of a new day.
    for sym in list(active_setups.keys()):
        if get_server_time().date() != current_date:
            del active_setups[sym]

    # Sleep 60 seconds before scanning again.
    time.sleep(60)
