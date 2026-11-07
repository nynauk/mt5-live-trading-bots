# MetaTrader 5 trading bots (Python)

Three live trading bots I wrote in 2025 to run my Asia-session strategies automatically through the official
`MetaTrader5` Python package. They are the execution side of the research in
[ict-strategy-backtests](https://github.com/nynauk/ict-strategy-backtests); the later, more careful bot with a
backtest parity test lives in [swiftpip-research](https://github.com/nynauk/swiftpip-research).

None of them contains login details. They attach to whichever account the MetaTrader 5 terminal on the same
machine is already logged into.

| Bot | Markets | Idea |
|---|---|---|
| `ifvg_bot.py` (Jun 2025, 200 lines) | 10 FX pairs | Marks the Asia range, waits for London or New York to sweep it, then sells when price closes back through a bullish FVG after a high is swept (and the reverse after a low), with the stop beyond the gap |
| `asia_fvg_ema_bot.py` (Jun 2025, 290 lines) | 27 symbols | Asia range sweep, with a 4-hour 50 EMA trend filter and a structure-shift check, entering on a limit order at the best unmitigated 3-minute FVG for a 3R target |
| `asia_sweep_htf_bot.py` (Sep 2025, 1,100 lines) | UK100, DAX | Asia sweep (03:00 to 10:00 server time) then an IFVG entry between 10:00 and 14:00, only after price taps a 1H/4H/daily FVG (no more than half filled) within its 38.2% to 61.8% band, on the correct side of the Asia midpoint, for 2R |

## What the larger bot handles

Most of `asia_sweep_htf_bot.py`'s length is the plumbing that a backtest does not need:

- **Broker time.** Session windows are computed from the server's own day start (taken from the daily bar's open
  time), so they stay right across daylight saving changes and different brokers.
- **Symbol metadata.** Prices are rounded to the instrument's tick size and volumes to its lot step using
  `Decimal`, stops respect the broker's minimum stop distance, and stops get a buffer of a few ticks.
- **Sizing.** Each trade risks 1% of the balance, worked out from tick value and stop distance.
- **Order hygiene.** One position per symbol, a cooldown after a rejected order, and pending limit orders
  cancelled if price reaches the target before they fill.
- **Daily stop.** Trading stops for the day after three losses.

## Run

```bash
python -m pip install MetaTrader5 pandas numpy
python asia_sweep_htf_bot.py    # MetaTrader 5 terminal open and logged into a demo account
```

Edit the config block at the top of each file for symbols (broker names vary, for example `UK100.Qraw`),
session hours in server time and risk.
