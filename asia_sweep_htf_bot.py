import MetaTrader5 as mt5
import pandas as pd
import time
import sys
import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP, getcontext
from functools import lru_cache
from collections import defaultdict

# ========= RUNTIME VERBOSITY / META CACHE =========
PRINT_META = False
FORCE_META_REFRESH = False

# ========= DECIMAL / PRECISION =========
getcontext().prec = 28
def _dec(x): return Decimal(str(x))

# ========= CONFIG =========

# --- DXY confirmation toggle (kept for future use; not used for UK100/DE30 confluence) ---
USE_DXY_CONFIRMATION = False
DXY_SYMBOL = "DXY.cash"

# --- Asia-only symbols (adjust to your broker names) ---
SYMBOLS_BASE = [
    # Asia profile (sweep 03–10, execute 10–14)
    "UK100.Qraw", "DE30.Qraw"
]
# Build the full symbol list (add DXY only when using the toggle)
SYMBOLS = SYMBOLS_BASE + ([DXY_SYMBOL] if USE_DXY_CONFIRMATION else [])

# Anchor symbol to determine "server midnight" via D1 open
BASE_SYMBOL_FOR_TIME = SYMBOLS_BASE[0]

# Working timeframe for LTF logic
TIMEFRAME = mt5.TIMEFRAME_M5
SESSION_TF_FALLBACKS = [mt5.TIMEFRAME_M5, mt5.TIMEFRAME_M15, mt5.TIMEFRAME_M30, mt5.TIMEFRAME_H1]
def tf_label(tf):
    return {
        mt5.TIMEFRAME_M1: "M1",
        mt5.TIMEFRAME_M5: "M5",
        mt5.TIMEFRAME_M15: "M15",
        mt5.TIMEFRAME_M30: "M30",
        mt5.TIMEFRAME_H1: "H1",
        mt5.TIMEFRAME_H4: "H4",
        mt5.TIMEFRAME_D1: "D1",
    }.get(tf, str(tf))

# HTF list for confluence (FVG-only)
HTF_LIST = [mt5.TIMEFRAME_H1, mt5.TIMEFRAME_H4, mt5.TIMEFRAME_D1]

# Trade/risk params
MAGIC = 20250806
DEVIATION = 20
RISK_PERCENT = 1.0      # % balance per trade
RR_TARGET = 2.0         # 2R target
SLEEP_SEC = 60
MIN_GAP_TICKS = 2       # min FVG size in ticks
MIN_LOOKBACK = 7        # IFVG pattern uses 7-candle lookback on M5
REQUIRED_BARS = MIN_LOOKBACK + 3

# ===== Asia session (server time) =====
ASIA_START_H, ASIA_END_H = 3, 10                 # Asia sweep window
EXEC_START_H, EXEC_END_H = 10, 14                # Execution window (10–14)

# Asia median filter
IFVG_FILTER_BY_ASIA_MEDIAN = True
IFVG_SIDE_BY_BIAS = True     # SELL→above median, BUY→below median
IFVG_ACCEPT_SIDE = "both"    # used when IFVG_SIDE_BY_BIAS=False

# HTF FVG Fibonacci pocket gate (allow closes inside up to 50%)
HTF_REQUIRE_FIB_GOLDEN = True
FIB_MIN, FIB_MAX = 0.382, 0.618

# Per-symbol tick overrides (rarely needed)
TICK_OVERRIDES = {
    # "DE30.Qraw": 1.0,
    # "UK100.Qraw": 1.0,
}

# Order send cooldown after a rejection
ORDER_FAIL_COOLDOWN_SEC = 300

# Extra stop buffer (beyond broker MSD) for SL/TP after entry legalization
STOP_BUFFER_TICKS = 3

# --- Cancel pendings if market hits TP before entry? ---
CANCEL_IF_TP_TOUCHED = True

# Symbols not tradable (confluence only)
DO_NOT_TRADE = {DXY_SYMBOL} if USE_DXY_CONFIRMATION else set()

# Daily risk stop
DAILY_MAX_LOSSES = 3
DAILY_STOP_ACTIVE = False
DAILY_STOP_DAY = None  # server-date when stop activated

# Per-symbol state
# {
#  "date": date, "sess_name": str, "sess_high": float, "sess_low": float,
#  "sess_start_ts": int, "sess_end_ts": int,
#  "bias": None|"buy"|"sell", "sweep_time": int|None, "stopped": bool,
#  "htf_ready": bool, "htf_reason": str, "htf_checked": bool,
#  "prev_levels": {...}, "htf_pois": {"bullish":[...], "bearish":[]},
#  "exec_start_ts": int, "exec_end_ts": int,
#  "asia_tf_used": str, "last_order_fail_ts": int,
#  "first_ifvg_time": int|None, "ifvg_committed": bool
# }
SYMBOL_STATE = {}

# ========= SERVER TIME HELPERS =========
def server_now_epoch():
    best = 0
    for s in SYMBOLS:
        t = mt5.symbol_info_tick(s)
        if t and getattr(t, "time", None):
            ts = int(t.time)
            best = max(best, ts)
    return best or int(time.time())

def server_day_start_epoch():
    try:
        rates = mt5.copy_rates_from_pos(BASE_SYMBOL_FOR_TIME, mt5.TIMEFRAME_D1, 0, 1)
        if rates is not None and len(rates) >= 1:
            return int(rates[0]['time'])  # server midnight via D1 open
    except Exception:
        pass
    now_ts = server_now_epoch()
    dt = datetime.fromtimestamp(now_ts)
    return int(datetime(dt.year, dt.month, dt.day, 0, 0, 0).timestamp())

def server_date_str():
    return datetime.fromtimestamp(server_day_start_epoch()).strftime('%Y-%m-%d')

def server_time_of(ts: int) -> str:
    day0 = server_day_start_epoch()
    delta = (ts - day0) % 86400
    h = int(delta // 3600); m = int((delta % 3600) // 60)
    return f"{h:02d}:{m:02d}"

def ts_at_hour_for_today(h):
    return server_day_start_epoch() + h*3600

def in_window(now_ts, start_ts, end_ts):
    return start_ts <= now_ts < end_ts

# ========= SYMBOL / PRICING HELPERS =========
@lru_cache(maxsize=256)
def _symbol_meta_cached(symbol):
    info = mt5.symbol_info(symbol)
    if not info: return None
    def g(obj, name, default=0.0): return getattr(obj, name, default)
    digits = int(g(info, "digits", 5))
    point = float(g(info, "point", 0.0001) or 0.0001)
    raw_tick_size = float(g(info, "trade_tick_size", 0.0) or 0.0)
    if symbol in TICK_OVERRIDES:
        tick_size = float(TICK_OVERRIDES[symbol])
    else:
        tick_size = raw_tick_size if raw_tick_size > 0 else (0.5 if digits <= 2 else point)
    tick_value = float(
        g(info, "trade_tick_value", 0.0)
        or g(info, "trade_tick_value_profit", 0.0)
        or g(info, "trade_tick_value_loss", 0.0)
        or 0.0
    )
    freeze_level_pts = int(g(info, "freeze_level", 0) or g(info, "trade_freeze_level", 0))
    stops_level_pts  = int(g(info, "trade_stops_level", 0) or g(info, "stops_level", 0))
    contract_size    = float(g(info, "trade_contract_size", g(info, "contract_size", 1.0)))
    vol_min = float(g(info, "volume_min", 0.01))
    vol_step = float(g(info, "volume_step", 0.01))
    vol_max = float(g(info, "volume_max", 100.0))
    return {
        "digits": digits, "point": point, "tick_size": tick_size, "tick_value": tick_value,
        "vol_min": vol_min, "vol_max": vol_max, "vol_step": vol_step,
        "stops_level": stops_level_pts, "freeze_level": freeze_level_pts, "contract_size": contract_size,
    }

def symbol_meta(symbol):
    if FORCE_META_REFRESH:
        try: _symbol_meta_cached.cache_clear()
        except Exception: pass
    return _symbol_meta_cached(symbol)

def round_price_to_tick(symbol, price):
    m = symbol_meta(symbol)
    if not m: return price
    ts = _dec(m["tick_size"]); d = m["digits"]
    if ts <= 0: return round(price, d)
    p  = _dec(price)
    steps = (p / ts).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    aligned = (steps * ts).quantize(_dec(10) ** -d)
    return float(aligned)

def round_volume(symbol, lots):
    m = symbol_meta(symbol)
    if not m: return 0.0
    step = m["vol_step"] or 0.01
    lots = max(m["vol_min"], min(m["vol_max"], lots))
    steps = int(lots / step)
    return max(m["vol_min"], min(m["vol_max"], steps * step))

def min_stop_distance_price(symbol):
    m = symbol_meta(symbol)
    if not m: return 0.0
    return m["stops_level"] * m["point"]

def get_current_spread(symbol):
    tick = mt5.symbol_info_tick(symbol)
    if not tick: return 0.0
    bid = float(getattr(tick, "bid", 0.0) or 0.0)
    ask = float(getattr(tick, "ask", 0.0) or 0.0)
    spr = max(0.0, ask - bid)
    return spr

# ========= DATA =========
def get_candles(symbol, count=REQUIRED_BARS+3):
    data = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, count)
    return pd.DataFrame(data) if data is not None else pd.DataFrame()

def get_candles_tf(symbol, timeframe, count=500):
    data = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    return pd.DataFrame(data) if data is not None else pd.DataFrame()

def get_candles_range(symbol, timeframe, start_ts, end_ts):
    if end_ts <= start_ts: return pd.DataFrame()
    dt_from = datetime.fromtimestamp(start_ts)
    dt_to   = datetime.fromtimestamp(end_ts)
    data = mt5.copy_rates_range(symbol, timeframe, dt_from, dt_to)
    df = pd.DataFrame(data) if data is not None else pd.DataFrame()
    if df.empty: return df
    return df[(df['time'] >= start_ts) & (df['time'] < end_ts)]

def get_session_bars_with_fallback(symbol, start_ts, end_ts):
    cutoff = min(end_ts, server_now_epoch())
    for tf in SESSION_TF_FALLBACKS:
        df = get_candles_range(symbol, tf, start_ts, cutoff)
        if df is not None and not df.empty:
            return df, tf
    return pd.DataFrame(), None

def get_session_high_low_with_fallback(symbol, start_ts, end_ts):
    df, tf = get_session_bars_with_fallback(symbol, start_ts, end_ts)
    if df is None or df.empty: return None, None, None
    return float(df['high'].max()), float(df['low'].min()), tf

def has_open_position(symbol):
    poss = mt5.positions_get(symbol=symbol) or []
    return len(poss) > 0

# ========= PREV LEVELS =========
def compute_prev_levels(symbol):
    out = {}
    ddf = get_candles_tf(symbol, mt5.TIMEFRAME_D1, count=3)
    if not ddf.empty and len(ddf) >= 2:
        prev = ddf.iloc[-2]
        out.update({"PDH": float(prev['high']), "PDL": float(prev['low'])})
    wdf = get_candles_tf(symbol, mt5.TIMEFRAME_W1, count=3)
    if not wdf.empty and len(wdf) >= 2:
        prev = wdf.iloc[-2]
        out.update({"PWH": float(prev['high']), "PWL": float(prev['low'])})
    mdf = get_candles_tf(symbol, mt5.TIMEFRAME_MN1, count=3)
    if not mdf.empty and len(mdf) >= 2:
        prev = mdf.iloc[-2]
        out.update({"PMH": float(prev['high']), "PML": float(prev['low'])})
    return out

# ========= HTF FVGs (allow closes inside up to 50%) + Fib pocket =========
def scan_fvg_zones(df, tf_name=""):
    zones = []
    if len(df) < 3: return zones
    for i in range(2, len(df)):
        c0 = df.iloc[i-2]; c2 = df.iloc[i]
        if c0['high'] < c2['low']:
            z_low = float(c0['high']); z_high = float(c2['low'])
            zones.append({"kind":"bullish","low":z_low,"high":z_high,"i":i,"time":int(c2['time']),"src":"FVG","tf":tf_name})
        if c0['low'] > c2['high']:
            z_low = float(c2['high']); z_high = float(c0['low'])
            zones.append({"kind":"bearish","low":z_low,"high":z_high,"i":i,"time":int(c2['time']),"src":"FVG","tf":tf_name})
    return zones

def is_wick_only_mitigated_ok(df, zone, start_index, max_penetration=0.50):
    """
    Allow closes inside the HTF FVG, but invalidate if any close goes deeper
    than `max_penetration` (default 50%) into the zone OR closes beyond the
    wrong side (bearish: close > high; bullish: close < low).
    """
    if df.empty:
        return False

    zl, zh = float(zone["low"]), float(zone["high"])
    kind   = zone["kind"]
    depth  = zh - zl
    if depth <= 0:
        return False

    if kind == "bearish":
        hard_limit = zh
        soft_limit = zl + depth * max_penetration
        for i in range(start_index + 1, len(df) - 1):
            cl = float(df.iloc[i]['close'])
            if cl > hard_limit:
                return False
            if zl <= cl <= zh and cl > soft_limit:
                return False
        return True
    else:  # bullish
        hard_limit = zl
        soft_limit = zh - depth * max_penetration
        for i in range(start_index + 1, len(df) - 1):
            cl = float(df.iloc[i]['close'])
            if cl < hard_limit:
                return False
            if zl <= cl <= zh and cl < soft_limit:
                return False
        return True

def fib_bounds_for_zone(zone):
    low = float(zone["low"]); high = float(zone["high"])
    depth = high - low
    fib_low  = low + FIB_MIN * depth
    fib_high = low + FIB_MAX * depth
    return fib_low, fib_high

def first_tap_in_fib_range(df_ltf, zone, since_epoch):
    w = df_ltf[df_ltf['time'] >= since_epoch]
    if w.empty: return None, None
    zl, zh = zone["low"], zone["high"]
    fib_l, fib_h = fib_bounds_for_zone(zone)
    fib_l = max(fib_l, zl); fib_h = min(fib_h, zh)
    for _, r in w.iterrows():
        hi = float(r['high']); lo = float(r['low'])
        if hi >= fib_l and lo <= fib_h:
            tap_price = max(zl, min(zh, float(r['close'])))
            return int(r['time']), tap_price
    return None, None

def htf_pois_for_symbol(symbol):
    pois = {"bullish": [], "bearish": []}
    for tf in HTF_LIST:
        df = get_candles_tf(symbol, tf, count=400)
        if df.empty or len(df) < 50: continue
        tf_name = {mt5.TIMEFRAME_H1:"H1", mt5.TIMEFRAME_H4:"H4", mt5.TIMEFRAME_D1:"D1"}.get(tf, str(tf))
        for z in scan_fvg_zones(df, tf_name):
            if is_wick_only_mitigated_ok(df, z, z["i"]):
                pois[z["kind"]].append(z)
    for k in ("bullish","bearish"):
        pois[k].sort(key=lambda x: x["time"], reverse=True)
    return pois

def zone_outside_session(zone, sess_high, sess_low, bias):
    if bias == "sell":   # want bearish zone fully above the Asia high
        return zone["kind"] == "bearish" and zone["low"] > sess_high
    else:                # buy → bullish zone fully below the Asia low
        return zone["kind"] == "bullish" and zone["high"] < sess_low

# ========= Asia median filter =========
def asia_median_for_symbol(symbol):
    st = SYMBOL_STATE.get(symbol, {})
    hi = st.get("sess_high", float('nan'))
    lo = st.get("sess_low", float('nan'))
    if hi != hi or lo != lo:
        return None
    return 0.5 * (float(hi) + float(lo))

def ifvg_passes_asia_median(symbol, entry_price, bias):
    if not IFVG_FILTER_BY_ASIA_MEDIAN:
        return True
    median = asia_median_for_symbol(symbol)
    if median is None:
        return True
    if IFVG_SIDE_BY_BIAS:
        return entry_price >= median if bias == "sell" else entry_price <= median
    if IFVG_ACCEPT_SIDE == "above": return entry_price >= median
    if IFVG_ACCEPT_SIDE == "below": return entry_price <= median
    return True

# ========= Previous-level taps =========
def prev_levels_outside_session_untouched(symbol, st, direction):
    levels = st.get("prev_levels", {})
    sess_high, sess_low = st.get("sess_high"), st.get("sess_low")
    sess_start_ts, sess_end_ts = st.get("sess_start_ts"), st.get("sess_end_ts")
    df_sess, _ = get_session_bars_with_fallback(symbol, sess_start_ts, sess_end_ts)
    if df_sess is None: df_sess = pd.DataFrame()
    eligible = {}
    if direction == "sell":
        for name in ("PDH","PWH","PMH"):
            lvl = levels.get(name)
            if lvl is None: continue
            if lvl > sess_high:
                if df_sess.empty or not (df_sess['high'] >= lvl).any():
                    eligible[name] = float(lvl)
    else:
        for name in ("PDL","PWL","PML"):
            lvl = levels.get(name)
            if lvl is None: continue
            if lvl < sess_low:
                if df_sess.empty or not (df_sess['low'] <= lvl).any():
                    eligible[name] = float(lvl)
    return eligible

def first_tap_of_prev_levels_since(symbol, levels_dict, direction, since_ts, now_ts):
    if not levels_dict: return None
    df = get_candles_range(symbol, TIMEFRAME, since_ts, now_ts)
    if (df is None or df.empty) and TIMEFRAME != mt5.TIMEFRAME_M1:
        df = get_candles_range(symbol, mt5.TIMEFRAME_M1, since_ts, now_ts)
    if df is None or df.empty: return None
    for _, r in df.sort_values('time').iterrows():
        if direction == "sell":
            for name, lvl in levels_dict.items():
                if r['high'] >= lvl:
                    return {"name": name, "level": lvl, "time": int(r['time'])}
        else:
            for name, lvl in levels_dict.items():
                if r['low'] <= lvl:
                    return {"name": name, "level": lvl, "time": int(r['time'])}
    return None

# ========= HTF CONFLUENCE (FVG in fib pocket OR previous level tap) =========
def ensure_htf_confluence(symbol):
    st = SYMBOL_STATE.get(symbol)
    if not st or st["bias"] is None: return

    if not st.get("htf_checked", False):
        st["prev_levels"] = compute_prev_levels(symbol)
        st["htf_pois"] = htf_pois_for_symbol(symbol)  # FVGs with ≤50% allowed closes
        st["htf_checked"] = True

    if st.get("htf_ready", False): return

    sess_end = st["sess_end_ts"]
    now_ts = server_now_epoch()
    df_ltf = get_candles_range(symbol, TIMEFRAME, sess_end, now_ts)
    if (df_ltf is None or df_ltf.empty) and TIMEFRAME != mt5.TIMEFRAME_M1:
        df_ltf = get_candles_range(symbol, mt5.TIMEFRAME_M1, sess_end, now_ts)
    if df_ltf is None or df_ltf.empty: return

    # Option A: HTF FVG tap (outside session; tap must be in 0.382–0.618)
    side_key = "bearish" if st["bias"] == "sell" else "bullish"
    candidate_zones = [z for z in st.get("htf_pois", {}).get(side_key, [])
                       if zone_outside_session(z, st["sess_high"], st["sess_low"], st["bias"])]
    for z in candidate_zones[:12]:
        tap_time, tap_price = first_tap_in_fib_range(df_ltf, z, st["sess_end_ts"]) if HTF_REQUIRE_FIB_GOLDEN else (None, None)
        if tap_time is not None:
            fib_l, fib_h = fib_bounds_for_zone(z)
            st["htf_ready"]  = True
            st["htf_reason"] = (f"tap FVG {z['tf']} [{round(z['low'],5)}..{round(z['high'],5)}], "
                                f"fib[{round(fib_l,5)}..{round(fib_h,5)}] @ {server_time_of(tap_time)} ~{round(tap_price,5)} "
                                f"(outside {st['sess_name']})")
            st["htf_hit"] = {"src": z["src"], "tf": z["tf"], "kind": z["kind"],
                             "low": z["low"], "high": z["high"], "tap_time": tap_time, "tap_price": tap_price,
                             "fib_low": fib_l, "fib_high": fib_h}
            print(f"[{symbol}] ✅ HTF confluence: {st['htf_reason']}")
            print(f"[{symbol}] HTF POI side = {st['htf_hit']['kind'].upper()} | TF={z['tf']} | zone=[{round(z['low'],5)}..{round(z['high'],5)}]")
            return

    # Option B: previous level tap (outside session & untouched during session)
    eligible_levels = prev_levels_outside_session_untouched(symbol, st, st["bias"])
    hit = first_tap_of_prev_levels_since(symbol, eligible_levels, st["bias"], st["sess_end_ts"], now_ts)
    if hit:
        st["htf_ready"] = True
        st["htf_reason"] = (f"tap {hit['name']}@{round(hit['level'],5)} at {server_time_of(hit['time'])} "
                            f"(outside {st['sess_name']}, untouched during {st['sess_name']})")
        st["htf_hit"] = {"src":"PREV", "tf":"D1/W1/MN1",
                         "kind":("bearish" if st["bias"]=="sell" else "bullish"),
                         "low":hit["level"], "high":hit["level"],
                         "tap_time":hit["time"], "tap_price":hit["level"]}
        print(f"[{symbol}] ✅ HTF confluence: {st['htf_reason']}")
        print(f"[{symbol}] HTF POI side = {st['htf_hit']['kind'].upper()} | SRC=PREV | level={round(hit['level'],5)}")
        return

# ========= IFVG logic (first since session end) =========
def is_valid_gap(symbol, high, low):
    m = symbol_meta(symbol)
    if not m: return False
    ts = m["tick_size"]
    return (high - low) >= (MIN_GAP_TICKS * ts)

def _max_recent_high(df, end_idx, bars=7):
    x = df.iloc[max(0, end_idx - bars):end_idx]
    return float(x['high'].max()) if not x.empty else float('nan')

def _min_recent_low(df, end_idx, bars=7):
    x = df.iloc[max(0, end_idx - bars):end_idx]
    return float(x['low'].min()) if not x.empty else float('nan')

def find_first_ifvg_since(symbol, df, since_ts, bias, median_filter_fn=None):
    """
    Return first disrespect IFVG formed after since_ts, matching bias.
    ALWAYS returns a LIMIT entry at the relevant FVG edge.
    """
    if df is None or len(df) < 10:
        return None

    for i in range(7, len(df) - 1):   # iterate closed bars forward
        c4 = df.iloc[i]               # disrespect/last-closed
        c1 = df.iloc[i - 5]
        c3 = df.iloc[i - 3]
        t = int(c4['time'])
        if t < int(since_ts): continue

        # Bullish FVG → disrespect → SELL (entry @ gap_low)
        if float(c1['high']) < float(c3['low']):
            if bias != "sell": continue
            fvg_low  = float(c1['high'])
            fvg_high = float(c3['low'])
            if is_valid_gap(symbol, fvg_high, fvg_low) and float(c4['close']) < fvg_low:
                entry = fvg_low
                if median_filter_fn and not median_filter_fn(symbol, entry, "sell"):
                    continue
                swing_high = _max_recent_high(df, i, bars=MIN_LOOKBACK)
                sl_raw = swing_high
                if not (sl_raw > entry):  # SELL must have SL above entry
                    continue
                return {
                    "direction": "sell", "when": t,
                    "gap_low": fvg_low, "gap_high": fvg_high,
                    "entry_price": entry, "sl": sl_raw
                }

        # Bearish FVG → disrespect → BUY (entry @ gap_high)
        if float(c1['low']) > float(c3['high']):
            if bias != "buy": continue
            fvg_high = float(c1['low'])
            fvg_low  = float(c3['high'])
            if is_valid_gap(symbol, fvg_high, fvg_low) and float(c4['close']) > fvg_high:
                entry = fvg_high
                if median_filter_fn and not median_filter_fn(symbol, entry, "buy"):
                    continue
                swing_low = _min_recent_low(df, i, bars=MIN_LOOKBACK)
                sl_raw = swing_low
                if not (sl_raw < entry):  # BUY must have SL below entry
                    continue
                return {
                    "direction": "buy", "when": t,
                    "gap_low": fvg_low, "gap_high": fvg_high,
                    "entry_price": entry, "sl": sl_raw
                }
    return None

# ========= CANCEL PENDINGS IF TP TOUCHED =========
def cancel_pendings_if_tp_touched(symbol):
    """Cancel our IFVG LIMIT orders if price has already touched their TP before entry is filled."""
    if not CANCEL_IF_TP_TOUCHED:
        return

    pendings = mt5.orders_get(symbol=symbol) or []
    if not pendings:
        return

    tick = mt5.symbol_info_tick(symbol)
    bid = float(getattr(tick, "bid", 0.0) or 0.0) if tick else None
    ask = float(getattr(tick, "ask", 0.0) or 0.0) if tick else None
    now_ts = server_now_epoch()

    for o in pendings:
        try:
            if getattr(o, "magic", 0) != MAGIC:
                continue
            if o.type not in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
                continue

            tp    = float(getattr(o, "tp", 0.0) or 0.0)
            entry = float(getattr(o, "price", 0.0) or 0.0)
            if tp <= 0 or entry <= 0:
                continue

            # Fast live tick check
            live_hit = False
            if bid is not None and ask is not None:
                if o.type == mt5.ORDER_TYPE_BUY_LIMIT and bid >= tp:
                    live_hit = True
                if o.type == mt5.ORDER_TYPE_SELL_LIMIT and ask <= tp:
                    live_hit = True
            if live_hit:
                mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                side = "BUY" if o.type == mt5.ORDER_TYPE_BUY_LIMIT else "SELL"
                print(f"[{symbol}] ❎ Canceled {side} LIMIT (TP {tp} touched before fill, live).")
                continue

            # Fallback: bar-based check since order creation
            t0 = int(getattr(o, "time_setup", 0) or 0)
            if t0 <= 0:
                continue

            df = get_candles_range(symbol, TIMEFRAME, t0, now_ts)
            if (df is None or df.empty) and TIMEFRAME != mt5.TIMEFRAME_M1:
                df = get_candles_range(symbol, mt5.TIMEFRAME_M1, t0, now_ts)
            if df is None or df.empty:
                continue

            if o.type == mt5.ORDER_TYPE_BUY_LIMIT:
                touched = float(df['high'].max()) >= tp
            else:
                touched = float(df['low'].min()) <= tp

            if touched:
                mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                side = "BUY" if o.type == mt5.ORDER_TYPE_BUY_LIMIT else "SELL"
                print(f"[{symbol}] ❎ Canceled {side} LIMIT (TP {tp} touched before fill, bars).")

        except Exception as e:
            print(f"[{symbol}] Cancel-TP check ERROR: {e}")

# ========= SESSION / PRINT HELPERS =========
def compute_session_and_exec_windows():
    sess_name = "Asia(03–10)"
    sess_start = ts_at_hour_for_today(ASIA_START_H)
    sess_end   = ts_at_hour_for_today(ASIA_END_H)
    exec_start = ts_at_hour_for_today(EXEC_START_H)  # 10
    exec_end   = ts_at_hour_for_today(EXEC_END_H)    # 14
    return sess_name, sess_start, sess_end, exec_start, exec_end

def _print_session(symbol, label, start_ts, end_ts, hi, lo, tf_used=None):
    now_ts = server_now_epoch()
    start_str = f"{server_date_str()} {server_time_of(start_ts)}"
    end_str   = server_time_of(end_ts)
    suffix = f" ({tf_label(tf_used)})" if tf_used else ""
    if now_ts < start_ts:
        print(f"[{symbol}] {label} {start_str}–{end_str} | not started yet"); return
    if hi is None or lo is None:
        print(f"[{symbol}] {label} {start_str}–{end_str} | no data in window"); return
    median = (hi + lo) / 2.0
    print(f"[{symbol}] {label} {start_str}–{end_str} | HIGH={hi} LOW={lo} MID={median}{suffix}")

def ensure_today_state(symbol):
    day_start = server_day_start_epoch()
    today = datetime.fromtimestamp(day_start).date()
    st = SYMBOL_STATE.get(symbol)

    sess_name, sess_start, sess_end, exec_start, exec_end = compute_session_and_exec_windows()

    if (not st) or (st.get("date") != today) or (st.get("sess_start_ts") != sess_start) or (st.get("sess_end_ts") != sess_end):
        s_high, s_low, tf_used = get_session_high_low_with_fallback(symbol, sess_start, sess_end)
        SYMBOL_STATE[symbol] = {
            "date": today,
            "sess_name": sess_name,
            "sess_high": s_high if s_high is not None else float('nan'),
            "sess_low": s_low if s_low is not None else float('nan'),
            "sess_start_ts": sess_start, "sess_end_ts": sess_end,
            "bias": None, "sweep_time": None, "stopped": False,
            "htf_ready": False, "htf_reason": "", "htf_checked": False,
            "prev_levels": {}, "htf_pois": {"bullish":[], "bearish":[]},
            "exec_start_ts": exec_start, "exec_end_ts": exec_end,
            "asia_tf_used": tf_label(tf_used) if tf_used else "",
            "last_order_fail_ts": 0,
            "first_ifvg_time": None,
            "ifvg_committed": False,
        }
        _print_session(symbol, f"{sess_name.upper()} (TODAY)", sess_start, sess_end, s_high, s_low, tf_used)
        return s_high is not None
    return True

# ===== Sweep detection (Asia only) =====
def _first_session_sweep_since(df, sess_high, sess_low):
    if df is None or df.empty: return None, None
    for _, r in df.sort_values('time').iterrows():
        if r['high'] > sess_high: return "sell", int(r['time'])
        if r['low'] < sess_low:   return "buy",  int(r['time'])
    return None, None

def maybe_set_bias_after_sweep(symbol):
    st = SYMBOL_STATE.get(symbol)
    if not st: return
    sess_end_ts = st["sess_end_ts"]
    now_epoch = server_now_epoch()
    if now_epoch < sess_end_ts or st["bias"] is not None: return
    try:
        sess_high = float(st["sess_high"]); sess_low = float(st["sess_low"])
        if not (sess_high == sess_high and sess_low == sess_low): return
    except Exception:
        return
    tick = mt5.symbol_info_tick(symbol)
    if not tick: return
    if max(tick.bid, tick.ask) > sess_high:
        st["bias"] = "sell"; st["sweep_time"] = now_epoch
        print(f"[{symbol}] {st['sess_name']} HIGH swept → bias = SELL"); return
    if min(tick.bid, tick.ask) < sess_low:
        st["bias"] = "buy"; st["sweep_time"] = now_epoch
        print(f"[{symbol}] {st['sess_name']} LOW swept → bias = BUY"); return
    df = get_candles_range(symbol, TIMEFRAME, sess_end_ts, now_epoch)
    if (df is None or df.empty) and TIMEFRAME != mt5.TIMEFRAME_M1:
        df = get_candles_range(symbol, mt5.TIMEFRAME_M1, sess_end_ts, now_epoch)
    direction, t0 = _first_session_sweep_since(df, sess_high, sess_low)
    if direction:
        st["bias"] = direction; st["sweep_time"] = t0
        side = "HIGH" if direction == "sell" else "LOW"
        print(f"[{symbol}] {st['sess_name']} {side} swept at {server_time_of(t0)} → bias = {direction.UPPER()}")

def stop_symbol(symbol, reason=""):
    st = SYMBOL_STATE.get(symbol)
    if st: st["stopped"] = True
    try:
        pendings = mt5.orders_get(symbol=symbol) or []
        for o in pendings:
            if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
                mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
    except Exception:
        pass
    print(f"[{symbol}] ⛔ Stopped.{' ('+reason+')' if reason else ''}")

# ========= ORDER SEND DIAGNOSTICS =========
def can_trade_symbol(symbol):
    info = mt5.symbol_info(symbol)
    term = mt5.terminal_info()
    acc  = mt5.account_info()
    if not info:
        print(f"[{symbol}] ❌ symbol_info() is None."); return False
    if not info.visible:
        mt5.symbol_select(symbol, True)
    if not term or not getattr(term, "connected", False):
        print(f"[{symbol}] ❌ Terminal not connected."); return False
    if hasattr(term, "trade_allowed") and not term.trade_allowed:
        print(f"[{symbol}] ❌ Terminal trading not allowed."); return False
    if hasattr(term, "tradeapi_disabled") and term.tradeapi_disabled:
        print(f"[{symbol}] ❌ Terminal Trade API disabled."); return False
    if not acc:
        print(f"[{symbol}] ❌ No account_info()."); return False
    if hasattr(acc, "trade_allowed") and not acc.trade_allowed:
        print(f"[{symbol}] ❌ Account trading not allowed."); return False
    trade_allowed_attr = getattr(info, "trade_allowed", None)
    if (trade_allowed_attr is not None) and (not trade_allowed_attr):
        print(f"[{symbol}] ❌ Symbol trading disabled by broker."); return False
    if hasattr(info, "trade_mode") and info.trade_mode not in (
        mt5.SYMBOL_TRADE_MODE_FULL, mt5.SYMBOL_TRADE_MODE_LONGONLY, mt5.SYMBOL_TRADE_MODE_SHORTONLY):
        print(f"[{symbol}] ❌ Symbol trade_mode not allowed: {getattr(info,'trade_mode',None)}"); return False
    return True

def sanitize_comment(text, max_len=31):
    s = re.sub(r'[^A-Za-z0-9 _]', '', str(text))
    return s[:max_len].strip()

def build_order_comment(direction, mode="LIM"):
    prefix = 'B' if direction == 'buy' else 'S'
    return sanitize_comment(f"{prefix} IFVG {mode}")

def sanity_print(symbol, entry, sl, tp):
    is_finite = all(map(lambda x: (x == x) and (abs(x) != float('inf')), [entry, sl, tp]))
    print(f"[{symbol}] send-check | entry={entry} sl={sl} tp={tp} | finite={is_finite}")

# ========= ORDER / RISK =========
def calculate_dynamic_lot(symbol, entry, sl, risk_percent=RISK_PERCENT):
    acc = mt5.account_info(); m = symbol_meta(symbol)
    if not acc or not m:
        print(f"[{symbol}] Could not retrieve account/symbol info."); return 0.0
    entry = round_price_to_tick(symbol, entry); sl = round_price_to_tick(symbol, sl)
    distance = abs(entry - sl); ts, tv = m["tick_size"], m["tick_value"]
    if distance <= 0 or ts <= 0 or tv <= 0:
        print(f"[{symbol}] Missing tick meta or zero distance (ts={ts}, tv={tv}, dist={distance})."); return 0.0
    ticks = distance / ts
    risk_amount = acc.balance * (risk_percent / 100.0)
    cost_per_lot = ticks * tv
    if cost_per_lot <= 0:
        print(f"[{symbol}] Invalid cost_per_lot."); return 0.0
    lots_raw_needed = risk_amount / cost_per_lot
    lots = round_volume(symbol, lots_raw_needed)
    expected_risk = lots * ticks * tv
    achieved_pct = (expected_risk / acc.balance) * 100.0 if acc.balance > 0 else 0.0
    limited = lots < lots_raw_needed - 1e-12
    lim_note = " | LIMITED by broker max" if limited else ""
    print(f"[{symbol}] Target {risk_percent:.2f}%=${risk_amount:.2f} | ticks={ticks:.1f} | tick=${tv:.5f} | "
          f"lot={lots} (needed≈{lots_raw_needed:.4f}) | ≈${expected_risk:.2f} (~{achieved_pct:.2f}%)" + lim_note)
    return lots

def safe_order_send(symbol, req):
    res = mt5.order_send(req)
    if res is None:
        code, details = mt5.last_error()
        print(f"[{symbol}] ❌ order_send() returned None | last_error=({code}, {details}) | req={req}")
        return None
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"[{symbol}] ❌ Order REJECTED: retcode={res.retcode}, comment='{getattr(res, 'comment','')}', request={req}")
    return res

def legalize_pending_price(symbol, order_type, entry):
    m = symbol_meta(symbol); tick = mt5.symbol_info_tick(symbol)
    if not m or not tick: return round_price_to_tick(symbol, entry)
    ts  = m["tick_size"]; msd = min_stop_distance_price(symbol); bid, ask = float(tick.bid), float(tick.ask)
    if order_type == mt5.ORDER_TYPE_BUY_LIMIT:
        target = min(entry, bid - msd - ts); return round_price_to_tick(symbol, target)
    else:
        target = max(entry, ask + msd + ts); return round_price_to_tick(symbol, target)

def place_limit_order(direction, symbol, entry, sl_raw):
    """
    LIMIT orders only. We first legalize the entry vs Bid/Ask & broker MSD,
    then compute spread-offset SL, then re-validate SL/TP after legalization.
    TP is recomputed from (entry, spread-adjusted/validated SL) at RR_TARGET.
    """
    if symbol in DO_NOT_TRADE:
        print(f"[{symbol}] ℹ️ Confluence-only symbol. Skipping order placement."); return
    if DAILY_STOP_ACTIVE:
        print(f"[{symbol}] ⛔ Daily loss stop active — no new orders today."); return
    if not mt5.symbol_select(symbol, True):
        print(f"[{symbol}] Not available or hidden in Market Watch. Skipping."); return
    if not can_trade_symbol(symbol):
        return
    if has_open_position(symbol):
        print(f"[{symbol}] Position already open. Skipping new order."); return

    # Avoid duplicate pendings
    pendings = mt5.orders_get(symbol=symbol) or []
    if any(o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT) for o in pendings):
        print(f"[{symbol}] Pending already exists. Skipping new order.")
        return

    # Round incoming levels
    entry  = round_price_to_tick(symbol, entry)
    sl_raw = round_price_to_tick(symbol, sl_raw)

    m   = symbol_meta(symbol)
    if not m:
        print(f"[{symbol}] ❌ No symbol meta."); return
    ts   = m["tick_size"]
    msd  = min_stop_distance_price(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        print(f"[{symbol}] ❌ No live tick."); return
    bid, ask = float(tick.bid), float(tick.ask)

    # Choose order type and legalize entry BEFORE computing SL/TP
    if direction == "buy":
        order_type = mt5.ORDER_TYPE_BUY_LIMIT
        legalized_entry = legalize_pending_price(symbol, order_type, entry)
        if legalized_entry != entry:
            print(f"[{symbol}] BUY entry legalized {entry} → {legalized_entry} (Bid={bid}, MSD={msd}, ts={ts})")
        entry = legalized_entry
    else:
        order_type = mt5.ORDER_TYPE_SELL_LIMIT
        legalized_entry = legalize_pending_price(symbol, order_type, entry)
        if legalized_entry != entry:
            print(f"[{symbol}] SELL entry legalized {entry} → {legalized_entry} (Ask={ask}, MSD={msd}, ts={ts})")
        entry = legalized_entry

    # Basic proximity to Bid/Ask (pending placement rule)
    if order_type == mt5.ORDER_TYPE_BUY_LIMIT and entry >= bid - msd:
        print(f"[{symbol}] ❌ buy limit too close/above Bid after legalization"); return
    if order_type == mt5.ORDER_TYPE_SELL_LIMIT and entry <= ask + msd:
        print(f"[{symbol}] ❌ sell limit too close/below Ask after legalization"); return

    # Now compute spread-offset SL, then make sure it's still on the correct side AFTER entry moved
    spread = get_current_spread(symbol)

    if direction == "buy":
        # BUY: SL below entry; offset by spread (farther away)
        sl = round_price_to_tick(symbol, sl_raw - spread)
        # If SL ended up on/above entry, bump it to entry - (msd + buffer)
        if sl >= entry:
            bump = msd + STOP_BUFFER_TICKS * ts
            new_sl = round_price_to_tick(symbol, entry - bump)
            print(f"[{symbol}] BUY SL bumped {sl} → {new_sl} to restore side/dist (entry={entry}, msd={msd})")
            sl = new_sl
        if sl >= entry:
            print(f"[{symbol}] ❌ BUY invalid SL side after bumping (sl={sl} >= entry={entry})."); return
        tp = round_price_to_tick(symbol, entry + RR_TARGET * (entry - sl))
    else:
        # SELL: SL above entry; offset by spread (farther away)
        sl = round_price_to_tick(symbol, sl_raw + spread)
        # If SL ended up on/below entry, bump it to entry + (msd + buffer)
        if sl <= entry:
            bump = msd + STOP_BUFFER_TICKS * ts
            new_sl = round_price_to_tick(symbol, entry + bump)
            print(f"[{symbol}] SELL SL bumped {sl} → {new_sl} to restore side/dist (entry={entry}, msd={msd})")
            sl = new_sl
        if sl <= entry:
            print(f"[{symbol}] ❌ SELL invalid SL side after bumping (sl={sl} <= entry={entry})."); return
        tp = round_price_to_tick(symbol, entry - RR_TARGET * (sl - entry))

    # Final distance checks vs broker MSD (from entry) with buffer
    min_dist = msd + STOP_BUFFER_TICKS * ts
    if abs(entry - sl) < min_dist:
        print(f"[{symbol}] ❌ SL too close to entry after legalization (|{entry - sl}| < {min_dist})."); return
    if abs(tp - entry) < min_dist:
        print(f"[{symbol}] ❌ TP too close to entry after legalization (|{tp - entry}| < {min_dist})."); return

    # Lot sizing on the final distances
    lots = calculate_dynamic_lot(symbol, entry, sl)
    if lots <= 0:
        print(f"[{symbol}] ❌ Invalid lot size after broker limits. Skipping."); return

    sanity_print(symbol, entry, sl, tp)
    req = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": lots,
        "type": order_type,
        "price": entry,
        "sl": sl,
        "tp": tp,
        "deviation": DEVIATION,
        "magic": MAGIC,
        "type_time": mt5.ORDER_TIME_GTC,  # GTC; we auto-cancel if TP touched first
        "comment": build_order_comment(direction, mode="LIM"),
    }
    res = safe_order_send(symbol, req)
    if not res or res.retcode != mt5.TRADE_RETCODE_DONE:
        st = SYMBOL_STATE.get(symbol)
        if st is not None:
            st["last_order_fail_ts"] = server_now_epoch()
        return
    rr = (abs(tp - entry) / max(1e-12, abs(entry - sl)))
    print(f"[{symbol}] ✅ {direction.upper()} LIMIT @ {entry} | {lots} lots | RR≈{rr:.2f}")

# ========= DAILY LOSS STOP =========
def losses_today_count():
    day0_ts = server_day_start_epoch()
    now_ts = server_now_epoch()
    day0_dt = datetime.fromtimestamp(day0_ts)
    now_dt  = datetime.fromtimestamp(now_ts)
    try:
        deals = mt5.history_deals_get(day0_dt, now_dt) or []
    except Exception:
        deals = []
    profits_by_pos = defaultdict(float)
    for d in deals:
        try:
            if getattr(d, "magic", 0) == MAGIC:
                profits_by_pos[getattr(d, "position_id", 0)] += float(getattr(d, "profit", 0.0))
        except Exception:
            continue
    losses = sum(1 for pid, p in profits_by_pos.items() if pid and p < 0.0)
    return losses

def check_and_apply_daily_stop():
    global DAILY_STOP_ACTIVE, DAILY_STOP_DAY
    today = datetime.fromtimestamp(server_day_start_epoch()).date()
    if DAILY_STOP_DAY is not None and DAILY_STOP_DAY != today:
        DAILY_STOP_ACTIVE = False; DAILY_STOP_DAY = None
        print(f"[RISK] New server day {today}. Daily loss stop reset.")
    if DAILY_STOP_ACTIVE: return
    losses = losses_today_count()
    if losses >= DAILY_MAX_LOSSES:
        DAILY_STOP_ACTIVE = True; DAILY_STOP_DAY = today
        print(f"[RISK] ⛔ Daily loss stop ACTIVATED — {losses} losses (≥{DAILY_MAX_LOSSES}). No new orders today.")

# ========= BOT LOOP =========
def run_bot():
    hdr = "🚀 IFVG bot — Asia sweep (03–10) + execute (10–14), HTF FVGs allow closes ≤50%, Fib 0.382–0.618, first IFVG, LIMIT-only, SL offset by spread, 1% risk."
    if CANCEL_IF_TP_TOUCHED:
        hdr += " Auto-cancel pendings if TP is touched before entry."
    print(hdr)

    # warmup
    for symbol in SYMBOLS:
        try:
            if mt5.symbol_select(symbol, True):
                ensure_today_state(symbol)
        except Exception as e:
            print(f"[{symbol}] Warm-up ERROR: {e}")

    try:
        while True:
            check_and_apply_daily_stop()
            now_ts = server_now_epoch()

            for symbol in SYMBOLS:
                try:
                    if not mt5.symbol_select(symbol, True):
                        print(f"[{symbol}] Not available or hidden in Market Watch. Skipping."); continue
                    if not ensure_today_state(symbol): continue

                    # NEW: cancel stale pendings that would have already hit TP without fill
                    cancel_pendings_if_tp_touched(symbol)

                    st = SYMBOL_STATE.get(symbol)
                    if st.get("stopped", False):
                        continue

                    if has_open_position(symbol):
                        stop_symbol(symbol, reason="first trade executed (position open)")
                        continue

                    if DAILY_STOP_ACTIVE:
                        continue

                    # If we already committed to the first IFVG, do not scan again
                    if st.get("ifvg_committed", False):
                        continue

                    # Bias from Asia sweep
                    maybe_set_bias_after_sweep(symbol)
                    st = SYMBOL_STATE.get(symbol)
                    if not st or st["bias"] is None:
                        continue

                    # One-off confluence: HTF FVG in fib pocket OR previous level tap
                    ensure_htf_confluence(symbol)
                    if not st.get("htf_ready", False):
                        continue

                    # Cooldown after failed send
                    if now_ts - st.get("last_order_fail_ts", 0) < ORDER_FAIL_COOLDOWN_SEC:
                        continue

                    # Execution window (Asia only)
                    if not in_window(now_ts, st["exec_start_ts"], st["exec_end_ts"]):
                        continue

                    # First IFVG only
                    if st.get("first_ifvg_time"):
                        continue

                    df = get_candles(symbol, count=1200)
                    if df.empty or len(df) < REQUIRED_BARS:
                        continue

                    median_filter_fn = ifvg_passes_asia_median
                    first_ifvg = find_first_ifvg_since(symbol, df, st["sess_end_ts"], st["bias"], median_filter_fn)

                    # Direction must match bias (safety)
                    if first_ifvg and first_ifvg["direction"] != st["bias"]:
                        print(f"[{symbol}] ⚠️ IFVG {first_ifvg['direction'].upper()} != bias {st['bias'].upper()} — skipping.")
                        first_ifvg = None

                    if first_ifvg:
                        print(f"[{symbol}] First IFVG ({first_ifvg['direction'].upper()}) at {server_time_of(first_ifvg['when'])} "
                              f"| edge-entry | gap=[{first_ifvg['gap_low']:.5f}..{first_ifvg['gap_high']:.5f}]")
                        st["first_ifvg_time"] = first_ifvg["when"]

                        dirn  = first_ifvg["direction"]
                        entry = first_ifvg["entry_price"]
                        slraw = first_ifvg["sl"]

                        # LIMIT only, SL offset by spread & fully validated after entry legalization
                        place_limit_order(dirn, symbol, entry, slraw)

                        # Lock to this first setup — never re-target later
                        st["ifvg_committed"] = True

                except Exception as e:
                    print(f"[{symbol}] ERROR: {e}")

            time.sleep(SLEEP_SEC)
    except KeyboardInterrupt:
        print("Bot manually stopped.")

# ========= PRINT TODAY'S SESSION RANGES =========
def print_todays_ranges_now():
    for symbol in SYMBOLS:
        try:
            if mt5.symbol_select(symbol, True):
                ensure_today_state(symbol)
        except Exception as e:
            print(f"[{symbol}] Warm-up ERROR: {e}")

def debug_trading_status():
    term = mt5.terminal_info()
    acc  = mt5.account_info()
    print(
        f"[DEBUG] Terminal connected={getattr(term,'connected','n/a')}, "
        f"trade_allowed={getattr(term,'trade_allowed','n/a')}, "
        f"tradeapi_disabled={getattr(term,'tradeapi_disabled','n/a')}"
    )
    if acc:
        print(
            f"[DEBUG] Account login={getattr(acc,'login','n/a')}, "
            f"server={getattr(acc,'server','n/a')}, "
            f"trade_allowed={getattr(acc,'trade_allowed','n/a')}, "
            f"company={getattr(acc,'company','n/a')}"
        )
    dbg_syms = SYMBOLS_BASE.copy()
    if USE_DXY_CONFIRMATION:
        dbg_syms.append(DXY_SYMBOL)
    for sym in dbg_syms:
        info = mt5.symbol_info(sym)
        if not info:
            print(f"[DEBUG:{sym}] (no symbol_info)")
            continue
        d = info._asdict() if hasattr(info, "_asdict") else {}
        visible       = d.get("visible", getattr(info, "visible", "n/a"))
        trade_mode    = d.get("trade_mode", getattr(info, "trade_mode", "n/a"))
        trade_allowed = d.get("trade_allowed", getattr(info, "trade_allowed", "n/a")) \
                        if hasattr(info, "trade_allowed") else d.get("trade_allowed", "n/a")
        vol_min       = d.get("volume_min", getattr(info, "volume_min", "n/a"))
        vol_step      = d.get("volume_step", getattr(info, "volume_step", "n/a"))
        vol_max       = d.get("volume_max", getattr(info, "volume_max", "n/a"))
        print(
            f"[DEBUG:{sym}] visible={visible}, trade_mode={trade_mode}, "
            f"trade_allowed={trade_allowed}, vol(min/step/max)={vol_min}/{vol_step}/{vol_max}"
        )

# ========= MENU / MAIN =========
def main():
    if FORCE_META_REFRESH:
        try: _symbol_meta_cached.cache_clear()
        except Exception: pass
    while True:
        print("\n=== IFVG Bot (ASIA-ONLY) ===")
        print("1) Run (Asia sweep: 03–10; execute 10–14)")
        print("2) Exit")
        choice = input("Select an option: ").strip()
        if choice == "2":
            print("Exiting."); sys.exit(0)
        elif choice == "1":
            if not mt5.initialize():
                raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
            try:
                print_todays_ranges_now()
                debug_trading_status()
                run_bot()
            finally:
                mt5.shutdown()
        else:
            print("Invalid option. Please choose 1 or 2.")

if __name__ == "__main__":
    main()
