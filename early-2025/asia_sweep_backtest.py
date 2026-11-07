import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import datetime
import matplotlib.pyplot as plt

# -------------------------------
# 1. INITIALIZE MT5 & SETUP GLOBAL VARIABLES
# -------------------------------
if not mt5.initialize():
    print("MT5 initialization failed.")
    mt5.shutdown()
    exit()

# Define major forex pairs and only the M3 timeframe.
forex_pairs = ["EURUSD", "GBPUSD" ] #"USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "USDCHF"]
timeframes = {
    "M3": mt5.TIMEFRAME_M3,
    'M5': mt5.TIMEFRAME_M5
}

# Date range: from Oct 1, 2024 to now.
date_to = datetime.datetime.now()
date_from = datetime.datetime(2024, 10, 1)


# -------------------------------
# 2. HELPER FUNCTION: CALCULATE ATR
# -------------------------------
def calculate_atr(df, period=14):
    df['tr'] = np.maximum(
        df['high'] - df['low'],
        np.maximum(
            abs(df['high'] - df['close'].shift(1)),
            abs(df['low'] - df['close'].shift(1))
        )
    )
    df['atr'] = df['tr'].rolling(window=period).mean()
    return df


# -------------------------------
# 3. POSITION CLASS (Normal Strategy)
# -------------------------------
class Position:
    def __init__(self, open_datetime, open_price, order_type, stop_loss, take_profit):
        self.open_datetime = open_datetime
        self.open_price = open_price
        self.order_type = order_type  # 'buy' or 'sell'
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.close_datetime = None
        self.close_price = None
        self.profit_percent = None  # Profit expressed in risk multiples (R)
        self.status = 'open'

    def close_position(self, close_datetime, close_price):
        self.close_datetime = close_datetime
        self.close_price = close_price
        if self.order_type == 'buy':
            risk = self.open_price - self.stop_loss
            self.profit_percent = ((self.close_price - self.open_price) / risk) if risk != 0 else 0
        else:  # 'sell'
            risk = self.stop_loss - self.open_price
            self.profit_percent = ((self.open_price - self.close_price) / risk) if risk != 0 else 0
        self.status = 'closed'

    def as_dict(self):
        return {
            'open_datetime': self.open_datetime,
            'open_price': self.open_price,
            'order_type': self.order_type,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'close_datetime': self.close_datetime,
            'close_price': self.close_price,
            'profit_percent': self.profit_percent,
            'status': self.status,
        }


# -------------------------------
# 4. ASIA SWEEP STRATEGY CLASS (Fixed SL/TP, RR = 2)
# -------------------------------
class AsiaSweepStrategy:
    def __init__(self, df):
        self.df = df
        self.positions = []  # List to store trades

    def run(self):
        atr_multiplier = 1.5  # Multiplier for ATR-based stop loss
        unique_days = sorted(set(self.df.index.date))
        for day in unique_days:
            df_day = self.df[self.df.index.date == day]
            if df_day.empty:
                continue

            # Define Asia session (03:00–09:00 UTC)
            df_asia = df_day.between_time("03:00", "09:00")
            if df_asia.empty:
                continue
            asia_high = df_asia['high'].max()
            asia_low = df_asia['low'].min()

            # Look for a sweep event in 09:00–12:00 UTC.
            df_sweep = df_day.between_time("09:00", "12:00").reset_index()
            first_sweep = None
            for idx, row in df_sweep.iterrows():
                if row['high'] > asia_high:
                    first_sweep = {
                        'time': row['time'],
                        'sweep_type': 'upper',
                        'price': row['high'],
                        'asia_level': asia_high
                    }
                    break
                elif row['low'] < asia_low:
                    first_sweep = {
                        'time': row['time'],
                        'sweep_type': 'lower',
                        'price': row['low'],
                        'asia_level': asia_low
                    }
                    break
            if not first_sweep:
                continue

            # Set stop loss and take profit using the latest ATR value.
            atr_value = df_day['atr'].iloc[-1]
            if first_sweep['sweep_type'] == 'upper':
                stop_loss = asia_high + (atr_multiplier * atr_value)
                take_profit = first_sweep['price'] - 3 * (stop_loss - first_sweep['price'])
                order_type = 'sell'
            else:
                stop_loss = asia_low - (atr_multiplier * atr_value)
                take_profit = first_sweep['price'] + 3 * (first_sweep['price'] - stop_loss)
                order_type = 'buy'

            # Round prices.
            entry_price = round(first_sweep['price'], 5)
            stop_loss = round(stop_loss, 5)
            take_profit = round(take_profit, 5)

            # Simulate trade execution within 09:00–12:00 UTC.
            entry_time = first_sweep['time']
            trade = Position(entry_time, entry_price, order_type, stop_loss, take_profit)
            session_end = datetime.datetime.combine(day, datetime.time(12, 0))
            df_trade = df_day[(df_day.index >= entry_time) & (df_day.index <= session_end)]
            trade_executed = False

            for t, row in df_trade.iterrows():
                if order_type == 'sell':
                    if row['high'] >= trade.stop_loss:
                        trade.close_position(t, trade.stop_loss)
                        trade_executed = True
                        break
                    if row['low'] <= trade.take_profit:
                        trade.close_position(t, trade.take_profit)
                        trade_executed = True
                        break
                elif order_type == 'buy':
                    if row['low'] <= trade.stop_loss:
                        trade.close_position(t, trade.stop_loss)
                        trade_executed = True
                        break
                    if row['high'] >= trade.take_profit:
                        trade.close_position(t, trade.take_profit)
                        trade_executed = True
                        break

            if not trade_executed and not df_trade.empty:
                last_time = df_trade.index[-1]
                last_close = df_trade.iloc[-1]['close']
                trade.close_position(last_time, last_close)
            self.positions.append(trade)
        return self.positions


# -------------------------------
# 5. HELPER FUNCTION: CALCULATE STREAKS
# -------------------------------
def get_longest_streaks(profit_list):
    longest_win = 0
    longest_loss = 0
    current_win = 0
    current_loss = 0
    for p in profit_list:
        if p > 0:
            current_win += 1
            longest_win = max(longest_win, current_win)
            current_loss = 0
        elif p < 0:
            current_loss += 1
            longest_loss = max(longest_loss, current_loss)
            current_win = 0
        else:
            current_win = 0
            current_loss = 0
    return longest_win, longest_loss


# -------------------------------
# 6. MAIN LOOP: BEST TIMEFRAME PER PAIR & OVERALL SUMMARY
# -------------------------------
best_timeframes = {}  # To store best timeframe per pair (will be M3 for all pairs).
global_best_trades = []  # To store all best trades across pairs.
pair_profit_dict = {}  # To store aggregated profit per pair.

# Overall aggregated stats.
overall_total_trades = 0
overall_winning_trades = 0
overall_losing_trades = 0
overall_total_profit_R = 0.0

for pair in forex_pairs:
    best_tf = None
    best_profit = -np.inf
    best_trades_for_pair = None

    # Only M3 is available.
    tf_label = "M3"
    tf_value = timeframes[tf_label]
    bars = mt5.copy_rates_range(pair, tf_value, date_from, date_to)
    if bars is None or len(bars) == 0:
        continue

    df = pd.DataFrame(bars)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df.set_index('time', inplace=True)
    df = calculate_atr(df)
    df.dropna(inplace=True)
    if df.empty:
        continue

    strategy = AsiaSweepStrategy(df)
    positions = strategy.run()
    if not positions:
        continue

    results = pd.DataFrame([p.as_dict() for p in positions])
    total_profit_R = results['profit_percent'].sum()

    if total_profit_R > best_profit:
        best_profit = total_profit_R
        best_tf = tf_label
        best_trades_for_pair = results

    best_timeframes[pair] = best_tf
    if best_trades_for_pair is not None:
        # Add pair information to each record.
        best_trades_for_pair['pair'] = pair
        pair_profit_dict[pair] = best_profit
        overall_total_trades += len(best_trades_for_pair)
        overall_winning_trades += len(best_trades_for_pair[best_trades_for_pair['profit_percent'] > 0])
        overall_losing_trades += len(best_trades_for_pair[best_trades_for_pair['profit_percent'] < 0])
        overall_total_profit_R += best_trades_for_pair['profit_percent'].sum()
        global_best_trades.extend(best_trades_for_pair.to_dict('records'))

if overall_total_trades > 0:
    overall_win_ratio = (overall_winning_trades / overall_total_trades) * 100
    overall_avg_profit_R = overall_total_profit_R / overall_total_trades
else:
    overall_win_ratio = 0
    overall_avg_profit_R = 0

# Compute global, time-dependent losing streak (sorting by close_datetime).
global_best_trades_sorted = sorted(global_best_trades, key=lambda x: x['close_datetime'] or datetime.datetime.max)
profit_list_global = [trade['profit_percent'] for trade in global_best_trades_sorted]
_, global_longest_loss = get_longest_streaks(profit_list_global)

# -------------------------------
# 7. ADDITIONAL METRICS: AVERAGE TRADE DURATION, MAXIMUM DRAWDOWN, AND TRADE EXECUTION TIMES
# -------------------------------
if global_best_trades:
    trades_df = pd.DataFrame(global_best_trades)
    trades_df = trades_df.dropna(subset=['open_datetime', 'close_datetime'])

    # Calculate trade duration in seconds.
    trades_df['open_datetime'] = pd.to_datetime(trades_df['open_datetime'])
    trades_df['close_datetime'] = pd.to_datetime(trades_df['close_datetime'])
    trades_df['duration_seconds'] = (trades_df['close_datetime'] - trades_df['open_datetime']).dt.total_seconds()

    # Average trade duration (in hours).
    avg_duration_hours = trades_df['duration_seconds'].mean() / 3600

    # Compute cumulative profit over time and maximum drawdown.
    trades_df = trades_df.sort_values(by='close_datetime')
    trades_df['cumulative_profit'] = trades_df['profit_percent'].cumsum()
    running_max = trades_df['cumulative_profit'].cummax()
    drawdown = running_max - trades_df['cumulative_profit']
    max_drawdown = drawdown.max()

    # Compute the range of trade execution times (open times) and the average execution time.
    min_open_time = trades_df['open_datetime'].min().time()
    max_open_time = trades_df['open_datetime'].max().time()
    trades_df['seconds_since_midnight'] = trades_df['open_datetime'].dt.hour * 3600 + \
                                          trades_df['open_datetime'].dt.minute * 60 + \
                                          trades_df['open_datetime'].dt.second
    avg_seconds = trades_df['seconds_since_midnight'].mean()
    avg_open_time = datetime.time(int(avg_seconds // 3600), int((avg_seconds % 3600) // 60), int(avg_seconds % 60))
else:
    avg_duration_hours = 0
    max_drawdown = 0
    min_open_time = None
    max_open_time = None
    avg_open_time = None

# -------------------------------
# 8. PRINT BEST TIMEFRAME PER PAIR & OVERALL SUMMARY
# -------------------------------
print("Best Timeframe per Pair (will be M3 for all pairs):")
for pair, tf in best_timeframes.items():
    print(f"  {pair}: {tf}")

print("\nOverall Summary (using best trades from M3 timeframe):")
print(f"  Total Trades             : {overall_total_trades}")
print(f"  Winning Trades           : {overall_winning_trades}")
print(f"  Losing Trades            : {overall_losing_trades}")
print(f"  Win Ratio                : {overall_win_ratio:.2f}%")
print(f"  Total Profit (R)         : {overall_total_profit_R:.2f}")
print(f"  Avg. Profit per Trade (R): {overall_avg_profit_R:.2f}")
print(f"  Global Time-Dependent Losing Streak: {global_longest_loss}")
print(f"  Average Trade Duration   : {avg_duration_hours:.2f} hours")
print(f"  Maximum Drawdown         : {max_drawdown:.2f} R")

if min_open_time and max_open_time and avg_open_time:
    print("\nTrade Execution Time Range (UTC):")
    print(f"  Earliest Execution: {min_open_time}")
    print(f"  Latest Execution  : {max_open_time}")
    print(f"  Average Execution : {avg_open_time}")

# -------------------------------
# 9. IDENTIFY MOST & LEAST PROFITABLE PAIR
# -------------------------------
if pair_profit_dict:
    best_pair = max(pair_profit_dict, key=pair_profit_dict.get)
    worst_pair = min(pair_profit_dict, key=pair_profit_dict.get)
    print("\nMost Profitable Pair:")
    print(f"  {best_pair} with {pair_profit_dict[best_pair]:.2f} R")
    print("\nWorst Performing Pair:")
    print(f"  {worst_pair} with {pair_profit_dict[worst_pair]:.2f} R")
else:
    print("\nNo pair profit data available.")

# -------------------------------
# 10. PRINT MOST & LEAST PROFITABLE WEEKDAYS
# -------------------------------
if global_best_trades:
    trades_df = pd.DataFrame(global_best_trades)
    trades_df = trades_df.dropna(subset=['close_datetime'])
    trades_df['weekday'] = pd.to_datetime(trades_df['close_datetime']).dt.day_name()
    weekday_profit = trades_df.groupby('weekday')['profit_percent'].sum().reset_index()
    most_profitable_weekdays = weekday_profit.sort_values(by='profit_percent', ascending=False)
    least_profitable_weekdays = weekday_profit.sort_values(by='profit_percent', ascending=True)

    print("\nMost Profitable Weekdays (aggregated profit in R):")
    for i, row in most_profitable_weekdays.iterrows():
        print(f"  {row['weekday']}: {row['profit_percent']:.2f} R")

    print("\nLeast Profitable Weekdays (aggregated profit in R):")
    for i, row in least_profitable_weekdays.iterrows():
        print(f"  {row['weekday']}: {row['profit_percent']:.2f} R")
else:
    print("\nNo trades available to compute weekday profits.")

# -------------------------------
# 11. PLOT THE MOST RECENT TRADE FOR EACH PAIR
# -------------------------------
# Build a dictionary mapping each pair to its most recent trade (based on close_datetime).
most_recent_trades = {}
for record in global_best_trades:
    pair = record.get('pair')
    if not pair:
        continue
    rec_close = pd.to_datetime(record['close_datetime'])
    if pair not in most_recent_trades or pd.to_datetime(most_recent_trades[pair]['close_datetime']) < rec_close:
        most_recent_trades[pair] = record

# For each pair, fetch the M3 bars for the trade day and plot the price series with markers for the trade open and close.
for pair, trade in most_recent_trades.items():
    trade_open = pd.to_datetime(trade['open_datetime'])
    trade_close = pd.to_datetime(trade['close_datetime'])
    # Define the day range for the trade (from midnight to midnight of that day)
    day_start = datetime.datetime.combine(trade_open.date(), datetime.time(0, 0))
    day_end = day_start + datetime.timedelta(days=1)
    bars = mt5.copy_rates_range(pair, mt5.TIMEFRAME_M3, day_start, day_end)
    if bars is None or len(bars) == 0:
        continue
    df_day = pd.DataFrame(bars)
    df_day['time'] = pd.to_datetime(df_day['time'], unit='s')
    df_day.set_index('time', inplace=True)

    plt.figure(figsize=(10, 6))
    plt.plot(df_day['close'], label='Price')
    plt.axvline(trade_open, color='green', linestyle='--', label='Trade Open')
    plt.axvline(trade_close, color='red', linestyle='--', label='Trade Close')
    plt.scatter(trade_open, trade['open_price'], color='green', zorder=5)
    plt.scatter(trade_close, trade['close_price'], color='red', zorder=5)
    plt.title(f"Most Recent Trade for {pair}")
    plt.xlabel("Time (UTC)")
    plt.ylabel("Price")
    plt.legend()
    plt.show()

mt5.shutdown()
