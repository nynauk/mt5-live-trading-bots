import MetaTrader5 as mt5
import pandas as pd
import time

# List of allowed symbols (Forex pairs, Commodities, Indices)
symbols = [
    # Forex Pairs
    "AUDCAD", "AUDCHF", "AUDJPY", "AUDNZD", "AUDUSD", "CADCHF", "CADJPY", "CHFJPY",
    "EURAUD", "EURCAD", "EURCHF", "EURGBP", "EURJPY", "EURNZD", "EURUSD", "GBPAUD",
    "GBPCAD", "GBPCHF", "GBPJPY", "GBPNZD", "GBPUSD", "NZDCAD", "NZDCHF", "NZDJPY",
    "NZDUSD", "USDCAD", "USDCHF", "USDJPY",
    # Commodities
    "XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD", "OIL", "WTI", "BRENT", "NGAS",
    # Indices
    "GER30", "SP500", "US100", "UK100", "DE40", "FRA40"
]

# Initialize connection to MT5
if not mt5.initialize():
    print("initialize() failed, error code =", mt5.last_error())
    quit()

print("Connected to MT5.")
print("Allowed symbols:", ', '.join(symbols))
print("Commands: buy, sell, buy limit, sell limit, buy stop, sell stop, take profit, stop loss, calculate pips, active trades, exit")

while True:
    symbol_input = input("\nEnter trading symbol (or type 'exit' to quit): ").strip().upper()
    if symbol_input == "EXIT":
        print("Exiting loop...")
        break

    if symbol_input not in symbols:
        print("Symbol not allowed. Please choose from the allowed list.")
        continue

    if not mt5.symbol_select(symbol_input, True):
        print(f"symbol_select() failed for {symbol_input}")
        continue

    command = input("Enter command (buy/sell/buy limit/sell limit/buy stop/sell stop/take profit/stop loss/calculate pips/active trades): ").strip().lower()

    # Command to print active trades
    if command in ["active trades", "positions", "trades"]:
        positions = mt5.positions_get()
        if positions is None or len(positions) == 0:
            print("No active positions.")
        else:
            print("Active Positions:")
            for pos in positions:
                print(f"Ticket: {pos.ticket}, Symbol: {pos.symbol}, Type: {'BUY' if pos.type == mt5.POSITION_TYPE_BUY else 'SELL'}, Volume: {pos.volume}, Open Price: {pos.price_open:.5f}, SL: {pos.sl:.5f}, TP: {pos.tp:.5f}, Profit: {pos.profit:.2f}")
        continue

    tick = mt5.symbol_info_tick(symbol_input)
    if tick is None:
        print("Failed to get tick for", symbol_input)
        continue

    #--------------------
    # Market Orders Branch
    #--------------------
    if command in ["buy", "sell"]:
        try:
            stop_loss = float(input("Enter your desired stop loss price: ").strip())
        except ValueError:
            print("Invalid stop loss input. Please enter a numeric value.")
            continue
        try:
            risk_dollar = float(input("Enter your risk amount in account currency: ").strip())
        except ValueError:
            print("Invalid risk amount input. Please enter a numeric value.")
            continue

        if command == "buy":
            entry_price = tick.ask
            order_type = mt5.ORDER_TYPE_BUY
        else:
            entry_price = tick.bid
            order_type = mt5.ORDER_TYPE_SELL

        # Validate stop loss relative to entry price
        if order_type == mt5.ORDER_TYPE_BUY and stop_loss >= entry_price:
            print("Invalid stop loss. For buy orders, stop loss must be below the entry price.")
            continue
        elif order_type == mt5.ORDER_TYPE_SELL and stop_loss <= entry_price:
            print("Invalid stop loss. For sell orders, stop loss must be above the entry price.")
            continue

        info = mt5.symbol_info(symbol_input)
        if info is None:
            print("Failed to get symbol info for", symbol_input)
            continue

        pip_size = info.point * 10
        pip_difference = abs(entry_price - stop_loss) / pip_size
        if pip_difference == 0:
            print("Stop loss cannot be equal to the entry price.")
            continue

        pip_value_per_standard_lot = 10.0
        required_lots = risk_dollar / (pip_difference * pip_value_per_standard_lot)
        print(f"\nEntry Price: {entry_price:.5f}")
        print(f"Stop Loss: {stop_loss:.5f}")
        print(f"Pip Difference: {pip_difference:.2f} pips")
        print(f"Risking ${risk_dollar:.2f} requires ~{required_lots:.2f} standard lots based on risk per pip.")

        account = mt5.account_info()
        if account is None:
            print("Failed to get account info.")
            continue

        contract_size = info.trade_contract_size
        leverage = account.leverage
        margin_per_lot = (contract_size * entry_price) / leverage
        max_lots_by_margin = account.balance / margin_per_lot
        print(f"Account balance ${account.balance:.2f}, leverage {leverage}:")
        print(f"Maximum lots allowed by margin: {max_lots_by_margin:.2f}")

        if required_lots > max_lots_by_margin:
            print(f"Required lots ({required_lots:.2f}) exceed margin-based maximum ({max_lots_by_margin:.2f}). Adjusting...")
            required_lots = max_lots_by_margin

        volume_min = info.volume_min
        volume_max = info.volume_max
        volume_step = info.volume_step
        if required_lots < volume_min:
            print(f"Calculated lot size {required_lots:.2f} is below minimum ({volume_min}). Adjusting to minimum.")
            required_lots = volume_min
        elif required_lots > volume_max:
            print(f"Calculated lot size {required_lots:.2f} is above maximum ({volume_max}). Adjusting to maximum.")
            required_lots = volume_max
        required_lots = round(required_lots / volume_step) * volume_step
        print(f"Final lot size for order: {required_lots:.2f} standard lots.")

        confirm = input("Do you want to execute the trade with this lot size? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("Trade cancelled by user.")
            continue

        deviation = 25  # Adjusted deviation

        order_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol_input,
            "volume": required_lots,
            "type": order_type,
            "price": entry_price,
            "sl": stop_loss,
            "deviation": deviation,
            "magic": 234000,
            "comment": "PyTrade",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        max_attempts = 3
        attempt = 0
        while attempt < max_attempts:
            new_tick = mt5.symbol_info_tick(symbol_input)
            if new_tick is None:
                print("Failed to refresh tick.")
                break
            order_request["price"] = new_tick.ask if order_type == mt5.ORDER_TYPE_BUY else new_tick.bid
            if order_type == mt5.ORDER_TYPE_BUY and stop_loss >= order_request["price"]:
                print("New tick invalidates stop loss for buy order. Aborting trade.")
                break
            elif order_type == mt5.ORDER_TYPE_SELL and stop_loss <= order_request["price"]:
                print("New tick invalidates stop loss for sell order. Aborting trade.")
                break
            result = mt5.order_send(order_request)
            if result.retcode in (10004, 10021):
                print(f"Attempt {attempt+1}: retcode {result.retcode} encountered, retrying...")
                attempt += 1
                time.sleep(1)
                continue
            else:
                break

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            print("Order failed. Last error:", mt5.last_error())
        else:
            print("Order result:", result)

    #--------------------
    # Pending Orders Branch
    #--------------------
    elif command in ["buy limit", "sell limit", "buy stop", "sell stop"]:
        try:
            pending_price = float(input("Enter your desired pending order price: ").strip())
        except ValueError:
            print("Invalid pending order price input.")
            continue

        if command == "buy limit" and pending_price >= tick.ask:
            print("For a buy limit, pending price must be below the current ask price.")
            continue
        if command == "sell limit" and pending_price <= tick.bid:
            print("For a sell limit, pending price must be above the current bid price.")
            continue
        if command == "buy stop" and pending_price <= tick.ask:
            print("For a buy stop, pending price must be above the current ask price.")
            continue
        if command == "sell stop" and pending_price >= tick.bid:
            print("For a sell stop, pending price must be below the current bid price.")
            continue

        try:
            stop_loss = float(input("Enter your desired stop loss price: ").strip())
        except ValueError:
            print("Invalid stop loss input. Please enter a numeric value.")
            continue
        try:
            risk_dollar = float(input("Enter your risk amount in account currency: ").strip())
        except ValueError:
            print("Invalid risk amount input. Please enter a numeric value.")
            continue

        entry_price = pending_price
        if command == "buy limit":
            order_type = mt5.ORDER_TYPE_BUY_LIMIT
        elif command == "sell limit":
            order_type = mt5.ORDER_TYPE_SELL_LIMIT
        elif command == "buy stop":
            order_type = mt5.ORDER_TYPE_BUY_STOP
        elif command == "sell stop":
            order_type = mt5.ORDER_TYPE_SELL_STOP

        if order_type in [mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP]:
            if stop_loss >= entry_price:
                print("Invalid stop loss. For buy pending orders, stop loss must be below the pending order price.")
                continue
        else:
            if stop_loss <= entry_price:
                print("Invalid stop loss. For sell pending orders, stop loss must be above the pending order price.")
                continue

        info = mt5.symbol_info(symbol_input)
        if info is None:
            print("Failed to get symbol info for", symbol_input)
            continue

        pip_size = info.point * 10
        pip_difference = abs(entry_price - stop_loss) / pip_size
        if pip_difference == 0:
            print("Stop loss cannot be equal to the pending order price.")
            continue

        pip_value_per_standard_lot = 10.0
        required_lots = risk_dollar / (pip_difference * pip_value_per_standard_lot)
        print(f"\nPending Order Price: {entry_price:.5f}")
        print(f"Stop Loss: {stop_loss:.5f}")
        print(f"Pip Difference: {pip_difference:.2f} pips")
        print(f"Risking ${risk_dollar:.2f} requires ~{required_lots:.2f} standard lots based on risk per pip.")

        account = mt5.account_info()
        if account is None:
            print("Failed to get account info.")
            continue

        contract_size = info.trade_contract_size
        leverage = account.leverage
        margin_per_lot = (contract_size * entry_price) / leverage
        max_lots_by_margin = account.balance / margin_per_lot
        print(f"Account balance ${account.balance:.2f}, leverage {leverage}:")
        print(f"Maximum lots allowed by margin: {max_lots_by_margin:.2f}")

        if required_lots > max_lots_by_margin:
            print(f"Required lots ({required_lots:.2f}) exceed margin-based maximum ({max_lots_by_margin:.2f}). Adjusting...")
            required_lots = max_lots_by_margin

        volume_min = info.volume_min
        volume_max = info.volume_max
        volume_step = info.volume_step
        if required_lots < volume_min:
            print(f"Calculated lot size {required_lots:.2f} is below the minimum ({volume_min}). Adjusting to minimum.")
            required_lots = volume_min
        elif required_lots > volume_max:
            print(f"Calculated lot size {required_lots:.2f} is above the maximum ({volume_max}). Adjusting to maximum.")
            required_lots = volume_max
        required_lots = round(required_lots / volume_step) * volume_step
        print(f"Final lot size for pending order: {required_lots:.2f} standard lots.")

        confirm = input("Do you want to place the pending order with this lot size? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("Pending order cancelled by user.")
            continue

        deviation = 25  # Adjust as needed.
        pending_request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol_input,
            "volume": required_lots,
            "type": order_type,
            "price": entry_price,
            "sl": stop_loss,
            "deviation": deviation,
            "magic": 234000,
            "comment": "PyTrade Pending",
            "type_time": mt5.ORDER_TIME_GTC,
            "expiration": 0,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(pending_request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            print("Pending order failed. Last error:", mt5.last_error())
        else:
            print("Pending order result:", result)

    #--------------------
    # Other Commands: Modify SL/TP, Calculate Pips
    #--------------------
    elif command in ["take profit", "stop loss"]:
        positions = mt5.positions_get(symbol=symbol_input)
        if positions is None or len(positions) == 0:
            print("No open positions for symbol", symbol_input)
            continue
        position = positions[0]
        ticket = position.ticket
        position_type = position.type
        rates = mt5.copy_rates_from_pos(symbol_input, mt5.TIMEFRAME_M1, 0, 50)
        if rates is None:
            print("Failed to get historical data for computing standard deviation")
            continue
        df = pd.DataFrame(rates)
        std = df['close'].std()
        if command == "take profit":
            new_tp = tick.ask + std if position_type == mt5.POSITION_TYPE_BUY else tick.bid - std
            new_sl = position.sl
            print(f"Setting Take Profit for {symbol_input} to {new_tp:.5f}")
        else:
            new_sl = tick.bid - std if position_type == mt5.POSITION_TYPE_BUY else tick.ask + std
            new_tp = position.tp
            print(f"Setting Stop Loss for {symbol_input} to {new_sl:.5f}")
        mod_request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": symbol_input,
            "sl": new_sl,
            "tp": new_tp,
            "magic": 234000,
            "comment": "PyTrade",
        }
        mod_result = mt5.order_send(mod_request)
        print("Modification result:", mod_result)

    elif command in ["calculate pips", "calc pips", "pips"]:
        try:
            price1 = float(input("Enter first price: ").strip())
            price2 = float(input("Enter second price: ").strip())
        except ValueError:
            print("Invalid price input. Please enter numeric values.")
            continue
        info = mt5.symbol_info(symbol_input)
        if info is None:
            print("Failed to get symbol info for pip calculation.")
            continue
        pip_size = info.point * 10
        pip_diff = abs(price1 - price2) / pip_size
        print(f"Pip difference between {price1} and {price2} for {symbol_input} is ~{pip_diff:.1f} pips")
    else:
        print("Invalid command. Please try again.")

mt5.shutdown()
print("MT5 connection closed.")