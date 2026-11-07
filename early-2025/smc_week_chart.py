import MetaTrader5 as mt5
import pandas as pd
import datetime
import plotly.graph_objects as go
import numpy as np

# -------------------------------
# 1. CONNECT TO METATRADER5
# -------------------------------
if not mt5.initialize():
    print("MT5 initialization failed.")
    mt5.shutdown()
    exit()

# -------------------------------
# 2. SETTINGS & DATA FETCH
# -------------------------------
symbol = "EURUSD"
# Lower timeframe: 3-minute
timeframe_low = mt5.TIMEFRAME_M3
# Higher timeframe: 4-hour (for market structure and order blocks)
timeframe_high = mt5.TIMEFRAME_H4

date_to = datetime.datetime.now()
date_from = date_to - datetime.timedelta(days=7)

# Fetch lower timeframe data
bars_low = mt5.copy_rates_range(symbol, timeframe_low, date_from, date_to)
if bars_low is None or len(bars_low) == 0:
    print("No lower timeframe data retrieved.")
    mt5.shutdown()
    exit()

df_low = pd.DataFrame(bars_low)
df_low['time'] = pd.to_datetime(df_low['time'], unit='s')
df_low.set_index('time', inplace=True)
df_low_full = df_low.reset_index()  # For plotting the full week

# Fetch higher timeframe data
bars_high = mt5.copy_rates_range(symbol, timeframe_high, date_from, date_to)
if bars_high is None or len(bars_high) == 0:
    print("No higher timeframe data retrieved.")
    mt5.shutdown()
    exit()

df_high = pd.DataFrame(bars_high)
df_high['time'] = pd.to_datetime(df_high['time'], unit='s')
df_high.set_index('time', inplace=True)

# -------------------------------
# 3. HIGHER TIMEFRAME ANALYSIS: Detect Order Blocks
# -------------------------------
# We define order blocks in a simple way:
# - A bullish candle (close > open) followed immediately by a bearish candle -> Supply (sell) zone.
# - A bearish candle (close < open) followed immediately by a bullish candle -> Demand (buy) zone.
order_blocks = []  # Each order block is a dict with time, type ('supply' or 'demand'), high, and low.
df_high_reset = df_high.reset_index()
for i in range(1, len(df_high_reset) - 1):
    prev = df_high_reset.iloc[i - 1]
    curr = df_high_reset.iloc[i]
    next_ = df_high_reset.iloc[i + 1]

    # Bullish candle before a reversal: potential supply zone (for a sell setup)
    if prev['close'] > prev['open'] and next_['close'] < next_['open']:
        order_blocks.append({
            'time': prev['time'],
            'type': 'supply',
            'high': prev['high'],
            'low': prev['low']
        })
    # Bearish candle before a reversal: potential demand zone (for a buy setup)
    elif prev['close'] < prev['open'] and next_['close'] > next_['open']:
        order_blocks.append({
            'time': prev['time'],
            'type': 'demand',
            'high': prev['high'],
            'low': prev['low']
        })

# -------------------------------
# 4. LOWER TIMEFRAME ANALYSIS & SMC Confluence
# -------------------------------
unique_days = sorted(set(df_low.index.date))
all_shapes = []  # For plotting shapes (Asia levels, FVG boxes, order blocks)
extra_traces = []  # For markers and annotations

for day in unique_days:
    df_day = df_low[df_low.index.date == day]
    if df_day.empty:
        continue

    # --- Calculate Asia Session Levels (03:00–09:00 UTC) ---
    df_asia = df_day.between_time("03:00", "09:00")
    if df_asia.empty:
        continue
    asia_high = df_asia['high'].max()
    asia_low = df_asia['low'].min()

    # --- Detect Sweep Event in 09:00–12:00 UTC ---
    df_sweep = df_day.between_time("09:00", "12:00").reset_index()
    first_sweep = None
    for idx, row in df_sweep.iterrows():
        if row['high'] > asia_high:
            first_sweep = {
                'time': row['time'],
                'sweep_type': 'upper',  # Price above Asia high: sell setup.
                'price': row['high'],
                'asia_level': asia_high,
                'sweep_low': row['low']  # For FVG detection.
            }
            break
        elif row['low'] < asia_low:
            first_sweep = {
                'time': row['time'],
                'sweep_type': 'lower',  # Price below Asia low: buy setup.
                'price': row['low'],
                'asia_level': asia_low,
                'sweep_high': row['high']  # For FVG detection.
            }
            break

    # --- Detect Swing Points in 09:00–12:00 UTC (simple pivot method) ---
    swing_highs = []
    swing_lows = []
    for i in range(1, len(df_sweep) - 1):
        current = df_sweep.iloc[i]
        prev_candle = df_sweep.iloc[i - 1]
        next_candle = df_sweep.iloc[i + 1]
        if current['high'] > prev_candle['high'] and current['high'] > next_candle['high']:
            swing_highs.append({'time': current['time'], 'price': current['high']})
        if current['low'] < prev_candle['low'] and current['low'] < next_candle['low']:
            swing_lows.append({'time': current['time'], 'price': current['low']})

    # --- Determine Fair Value Gap (FVG) Based on Breakout Swing Points ---
    breakout_fvg = None
    if first_sweep:
        if first_sweep['sweep_type'] == 'upper':
            # Sell setup: focus on swing lows.
            swing_lows_after = [pt for pt in swing_lows if pt['time'] > first_sweep['time']]
            swing_lows_after = sorted(swing_lows_after, key=lambda x: x['time'])
            breakout_swing_low = None
            recent_swing_low = None
            for i in range(1, len(swing_lows_after)):
                if swing_lows_after[i]['price'] < swing_lows_after[i - 1]['price']:
                    recent_swing_low = swing_lows_after[i - 1]
                    breakout_swing_low = swing_lows_after[i]
                    break
            if recent_swing_low and breakout_swing_low:
                breakout_fvg = {
                    'type': 'upper',
                    'fvg_upper': recent_swing_low['price'],
                    'fvg_lower': breakout_swing_low['price'],
                    'recent_swing': recent_swing_low,
                    'breakout_swing': breakout_swing_low,
                    'gap': recent_swing_low['price'] - breakout_swing_low['price']
                }
        elif first_sweep['sweep_type'] == 'lower':
            # Buy setup: focus on swing highs.
            swing_highs_after = [pt for pt in swing_highs if pt['time'] > first_sweep['time']]
            swing_highs_after = sorted(swing_highs_after, key=lambda x: x['time'])
            breakout_swing_high = None
            recent_swing_high = None
            for i in range(1, len(swing_highs_after)):
                if swing_highs_after[i]['price'] > swing_highs_after[i - 1]['price']:
                    recent_swing_high = swing_highs_after[i - 1]
                    breakout_swing_high = swing_highs_after[i]
                    break
            if recent_swing_high and breakout_swing_high:
                breakout_fvg = {
                    'type': 'lower',
                    'fvg_upper': breakout_swing_high['price'],
                    'fvg_lower': recent_swing_high['price'],
                    'recent_swing': recent_swing_high,
                    'breakout_swing': breakout_swing_high,
                    'gap': breakout_swing_high['price'] - recent_swing_high['price']
                }

    # --- Check Confluence with Higher Timeframe Order Blocks ---
    confluence_order_block = None
    if breakout_fvg:
        for ob in order_blocks:
            # For a sell setup, look for a supply (order block) confluence;
            # for a buy setup, look for a demand confluence.
            if breakout_fvg['type'] == 'upper' and ob['type'] == 'supply':
                # Check if the FVG's upper swing is near the order block's high (within 0.2% tolerance)
                if abs(ob['high'] - breakout_fvg['recent_swing']['price']) / breakout_fvg['recent_swing'][
                    'price'] < 0.002:
                    confluence_order_block = ob
                    break
            elif breakout_fvg['type'] == 'lower' and ob['type'] == 'demand':
                if abs(ob['low'] - breakout_fvg['recent_swing']['price']) / breakout_fvg['recent_swing'][
                    'price'] < 0.002:
                    confluence_order_block = ob
                    break

    # --- Volume/Order Flow Confirmation ---
    # Check if the sweep candle has a volume spike (using tick_volume)
    avg_volume = df_asia['tick_volume'].mean()
    volume_spike = False
    if first_sweep:
        # Get the row corresponding to the sweep event time
        sweep_row = df_day[df_day.index == first_sweep['time']]
        if not sweep_row.empty and sweep_row['tick_volume'].iloc[0] > 1.5 * avg_volume:
            volume_spike = True

    # --- Determine Order Levels if all confluence criteria are met ---
    order_levels = None
    if breakout_fvg and confluence_order_block and volume_spike:
        # Entry at FVG midpoint
        limit_order = (breakout_fvg['fvg_upper'] + breakout_fvg['fvg_lower']) / 2.0

        if breakout_fvg['type'] == 'upper':  # Sell setup
            # Stop loss: slightly above the supply order block high (adding a small buffer)
            stop_loss = confluence_order_block['high'] * 1.001
            take_profit = asia_low  # Use Asia low as TP target
        elif breakout_fvg['type'] == 'lower':  # Buy setup
            stop_loss = confluence_order_block['low'] * 0.999
            take_profit = asia_high  # Use Asia high as TP target

        order_levels = {
            'limit_order': limit_order,
            'stop_loss': stop_loss,
            'take_profit': take_profit
        }

    # --- Simulate Trade Outcome (TP vs. Stop Out) ---
    trade_outcome = None  # "Take Profit", "Stopped Out", or "Open"
    outcome_time = None
    outcome_price = None
    if order_levels and breakout_fvg:
        entry_price = order_levels['limit_order']
        trade_type = breakout_fvg['type']  # 'upper' indicates sell, 'lower' indicates buy
        # Assume execution at breakout swing time
        execution_time = breakout_fvg['breakout_swing']['time']
        trade_df = df_day[df_day.index > execution_time]

        for idx, row in trade_df.iterrows():
            if trade_type == 'upper':  # Sell trade: want price to drop.
                condition_tp = row['low'] <= order_levels['take_profit']
                condition_sl = row['high'] >= order_levels['stop_loss']
            else:  # Buy trade: want price to rise.
                condition_tp = row['high'] >= order_levels['take_profit']
                condition_sl = row['low'] <= order_levels['stop_loss']

            if condition_tp and not condition_sl:
                trade_outcome = "Take Profit"
                outcome_time = idx
                outcome_price = order_levels['take_profit']
                break
            elif condition_sl and not condition_tp:
                trade_outcome = "Stopped Out"
                outcome_time = idx
                outcome_price = order_levels['stop_loss']
                break
            elif condition_tp and condition_sl:
                # Both conditions occur in the same candle.
                if trade_type == 'upper':
                    diff_tp = entry_price - order_levels['take_profit']
                    diff_sl = order_levels['stop_loss'] - entry_price
                else:
                    diff_tp = order_levels['take_profit'] - entry_price
                    diff_sl = entry_price - order_levels['stop_loss']
                if diff_tp < diff_sl:
                    trade_outcome = "Take Profit"
                    outcome_price = order_levels['take_profit']
                else:
                    trade_outcome = "Stopped Out"
                    outcome_price = order_levels['stop_loss']
                outcome_time = idx
                break

        if trade_outcome is None:
            trade_outcome = "Open"

    # --- Plotting Preparations for the Day ---
    period_start = datetime.datetime.combine(day, datetime.time(9, 0))
    period_end = datetime.datetime.combine(day, datetime.time(12, 0))

    # Asia session horizontal lines
    all_shapes.append({
        'type': 'line',
        'x0': period_start, 'x1': period_end,
        'y0': asia_high, 'y1': asia_high,
        'line': {'color': "RoyalBlue", 'width': 2, 'dash': "dash"}
    })
    all_shapes.append({
        'type': 'line',
        'x0': period_start, 'x1': period_end,
        'y0': asia_low, 'y1': asia_low,
        'line': {'color': "FireBrick", 'width': 2, 'dash': "dash"}
    })
    extra_traces.append(go.Scatter(
        x=[period_start],
        y=[asia_high],
        mode="text",
        text=[f"Asia High: {asia_high:.5f}"],
        textposition="top left",
        marker=dict(color="RoyalBlue")
    ))
    extra_traces.append(go.Scatter(
        x=[period_start],
        y=[asia_low],
        mode="text",
        text=[f"Asia Low: {asia_low:.5f}"],
        textposition="bottom left",
        marker=dict(color="FireBrick")
    ))

    # Mark the sweep event.
    if first_sweep:
        marker_color = "green" if first_sweep['sweep_type'] == 'upper' else "red"
        extra_traces.append(go.Scatter(
            x=[first_sweep['time']],
            y=[first_sweep['price']],
            mode="markers",
            marker=dict(color=marker_color, size=12, symbol="x"),
            name=f"First Sweep ({day})"
        ))

    # Mark swing points.
    if swing_highs:
        extra_traces.append(go.Scatter(
            x=[pt['time'] for pt in swing_highs],
            y=[pt['price'] for pt in swing_highs],
            mode="markers",
            marker=dict(symbol="triangle-up", color="green", size=8),
            name=f"Swing Highs ({day})"
        ))
    if swing_lows:
        extra_traces.append(go.Scatter(
            x=[pt['time'] for pt in swing_lows],
            y=[pt['price'] for pt in swing_lows],
            mode="markers",
            marker=dict(symbol="triangle-down", color="red", size=8),
            name=f"Swing Lows ({day})"
        ))

    # Mark the breakout FVG region if detected.
    if breakout_fvg:
        if breakout_fvg['type'] == 'upper':
            extra_traces.append(go.Scatter(
                x=[breakout_fvg['recent_swing']['time']],
                y=[breakout_fvg['recent_swing']['price']],
                mode="markers",
                marker=dict(symbol="star", color="blue", size=12),
                name=f"Recent Swing Low ({day})"
            ))
            extra_traces.append(go.Scatter(
                x=[breakout_fvg['breakout_swing']['time']],
                y=[breakout_fvg['breakout_swing']['price']],
                mode="markers",
                marker=dict(symbol="star", color="purple", size=12),
                name=f"Breakout Swing Low ({day})"
            ))
            fvg_upper = breakout_fvg['recent_swing']['price']
            fvg_lower = breakout_fvg['breakout_swing']['price']
            all_shapes.append({
                'type': 'rect',
                'xref': 'x',
                'yref': 'y',
                'x0': period_start, 'x1': period_end,
                'y0': fvg_lower, 'y1': fvg_upper,
                'fillcolor': "rgba(128,0,128,0.3)",
                'line': {'width': 0},
                'layer': 'below'
            })
            extra_traces.append(go.Scatter(
                x=[period_start],
                y=[(fvg_upper + fvg_lower) / 2],
                mode="text",
                text=[f"FVG: {fvg_upper:.5f}-{fvg_lower:.5f}"],
                textposition="middle left",
                marker=dict(color="purple")
            ))
        elif breakout_fvg['type'] == 'lower':
            extra_traces.append(go.Scatter(
                x=[breakout_fvg['recent_swing']['time']],
                y=[breakout_fvg['recent_swing']['price']],
                mode="markers",
                marker=dict(symbol="star", color="blue", size=12),
                name=f"Recent Swing High ({day})"
            ))
            extra_traces.append(go.Scatter(
                x=[breakout_fvg['breakout_swing']['time']],
                y=[breakout_fvg['breakout_swing']['price']],
                mode="markers",
                marker=dict(symbol="star", color="orange", size=12),
                name=f"Breakout Swing High ({day})"
            ))
            fvg_upper = breakout_fvg['breakout_swing']['price']
            fvg_lower = breakout_fvg['recent_swing']['price']
            all_shapes.append({
                'type': 'rect',
                'xref': 'x',
                'yref': 'y',
                'x0': period_start, 'x1': period_end,
                'y0': fvg_lower, 'y1': fvg_upper,
                'fillcolor': "rgba(255,165,0,0.3)",
                'line': {'width': 0},
                'layer': 'below'
            })
            extra_traces.append(go.Scatter(
                x=[period_start],
                y=[(fvg_upper + fvg_lower) / 2],
                mode="text",
                text=[f"FVG: {fvg_upper:.5f}-{fvg_lower:.5f}"],
                textposition="middle left",
                marker=dict(color="orange")
            ))

    # Mark higher timeframe order block (if confluence found)
    if confluence_order_block:
        if confluence_order_block['type'] == 'supply':
            color_ob = "magenta"
            label_ob = f"Supply OB ({confluence_order_block['time'].date()})"
        else:
            color_ob = "cyan"
            label_ob = f"Demand OB ({confluence_order_block['time'].date()})"
        # Draw a horizontal line representing the order block range.
        all_shapes.append({
            'type': 'rect',
            'xref': 'x',
            'yref': 'y',
            'x0': period_start, 'x1': period_end,
            'y0': confluence_order_block['low'], 'y1': confluence_order_block['high'],
            'fillcolor': f"rgba({255 if color_ob == 'magenta' else 0}, {0 if color_ob == 'magenta' else 255}, 150, 0.3)",
            'line': {'width': 0},
            'layer': 'below'
        })
        extra_traces.append(go.Scatter(
            x=[period_start],
            y=[(confluence_order_block['high'] + confluence_order_block['low']) / 2],
            mode="text",
            text=[label_ob],
            textposition="middle right",
            marker=dict(color=color_ob)
        ))

    # Mark Order Levels if computed.
    if order_levels:
        extra_traces.append(go.Scatter(
            x=[period_start],
            y=[order_levels['limit_order']],
            mode="markers+text",
            marker=dict(symbol="diamond", color="black", size=12),
            text=[f"Limit: {order_levels['limit_order']:.5f}"],
            textposition="bottom right",
            name=f"Limit Order ({day})"
        ))
        extra_traces.append(go.Scatter(
            x=[period_start],
            y=[order_levels['stop_loss']],
            mode="markers+text",
            marker=dict(symbol="x", color="red", size=12),
            text=[f"Stop: {order_levels['stop_loss']:.5f}"],
            textposition="top right",
            name=f"Stop Loss ({day})"
        ))
        extra_traces.append(go.Scatter(
            x=[period_start],
            y=[order_levels['take_profit']],
            mode="markers+text",
            marker=dict(symbol="circle", color="green", size=12),
            text=[f"TP: {order_levels['take_profit']:.5f}"],
            textposition="top left",
            name=f"Take Profit ({day})"
        ))

        # Mark the trade outcome.
        if trade_outcome != "Open" and outcome_time is not None:
            if trade_outcome == "Take Profit":
                marker_symbol = "star-triangle-up"
                marker_color = "green"
            else:
                marker_symbol = "x"
                marker_color = "red"
            extra_traces.append(go.Scatter(
                x=[outcome_time],
                y=[outcome_price],
                mode="markers+text",
                marker=dict(symbol=marker_symbol, color=marker_color, size=14),
                text=[trade_outcome],
                textposition="bottom center",
                name=f"Trade Outcome ({day})"
            ))
        else:
            extra_traces.append(go.Scatter(
                x=[period_end],
                y=[order_levels['limit_order']],
                mode="markers+text",
                marker=dict(symbol="circle", color="gray", size=12),
                text=["Open"],
                textposition="bottom center",
                name=f"Trade Outcome ({day})"
            ))

# -------------------------------
# 5. PLOT THE FULL-WEEK CHART WITH OVERLAYS
# -------------------------------
fig = go.Figure()

fig.add_trace(go.Candlestick(
    x=df_low_full['time'],
    open=df_low_full['open'],
    high=df_low_full['high'],
    low=df_low_full['low'],
    close=df_low_full['close'],
    name="EURUSD 3-min"
))

for trace in extra_traces:
    fig.add_trace(trace)

fig.update_layout(
    shapes=all_shapes,
    title="Smart Money Confluence Breakout Strategy: EURUSD 3-min with 4H Order Blocks, FVG, & SMC Confluence",
    xaxis_title="Time (UTC)",
    yaxis_title="Price",
    xaxis_rangeslider_visible=False,
    template="plotly_white"
)

fig.show()

# -------------------------------
# SHUTDOWN CONNECTION
# -------------------------------
mt5.shutdown()