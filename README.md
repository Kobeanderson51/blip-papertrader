# Paper Trader

Trades **fake money** ($50 start, $100 goal) across 10 Solana meme coins (WIF, BONK, FARTCOIN, PENGU, POPCAT, MEW, BOME, PNUT, MOODENG, GOAT) against **live prices** (DexScreener, free, no key) and charges realistic costs (0.25% swap fee, network fee, slippage scaled to pool liquidity). The point is to find out whether a strategy makes money *after costs* before any real money is involved.

## Run it

Needs Python 3.10+ and nothing else.

```
python bot.py once        # one price check
python bot.py run         # keep running (checks every 60s)
python bot.py report      # how the $10 paper account is doing
python bot.py stop        # EMERGENCY STOP: halts trading, keeps all logs
python bot.py resume      # asks you to type RESUME
python bot.py reset       # asks you to type RESET; old logs are kept as .bak
python bot.py simulate    # offline self-test on random prices
```

Settings live in `config.json`. Strategy since Nov 17 2026: **rsi_dip** on closed 1-hour candles: buy when RSI(14) crosses below 20, sell when RSI closes above 50, at an 8% stop, or after 24h; max 6 positions. It replaced EMA momentum, which lost on every setting tested (see `research/`). Kill rules stop trading automatically at a 20% drawdown from the account high, or if P&L after costs is not positive after 30 closed trades. Every trade is logged to `data/trades.jsonl`, every stop/resume/error to `data/events.jsonl`.

## The $10 live mode (locked)

Live mode refuses to start unless **all** of these are true:

1. `live.enabled` is `true` in config.json.
2. The environment variable `LIVE_TRADING` is set to `I_ACCEPT_LOSING_IT`.
3. Caps are ≤ $10 total and ≤ $5 per trade.
4. At least 50 closed paper trades.
5. Paper P&L **after costs** is positive.
6. Emergency Stop is off.

Even when they all pass, real swap execution isn't connected yet. It gets wired in (Jupiter swap API, a separate burner wallet that only ever holds $10) only after the paper record earns it and you approve. Your personal wallet and seed phrase are never involved.

## Honest expectations

On random prices (`simulate`), the bot lost about 72% of its $10, mostly to fees, over 195 trades. Frequent small trades pay costs every time. If paper results look like that on real prices, the right move is to change the strategy or trade less, not to add real money.

## Running on GitHub (this repo)

A scheduled GitHub Action runs `bot.py once` every 15 minutes, so each "tick" is a 15-minute candle. The paper account and `data/status.json` are committed back to this repo after every run (that also keeps the schedule from going idle).

- **See results:** Actions tab → latest "Paper trade" run → the summary shows the report. The full trade log is attached as `trade-log`.
- **Run it now:** Actions → Paper trade → Run workflow.
- **EMERGENCY STOP:** add a file named `EMERGENCY_STOP` (any content) to the repo root. Delete it to resume. Or disable the workflow in the Actions tab.
- **Change the strategy:** edit `config.json`.

GitHub pauses scheduled workflows in public repos after 60 days without commits; any commit re-enables them.


## Notifications

When the paper account reaches the goal ($100), or falls to $25, the workflow opens a GitHub issue in this repo once. GitHub notifies you (app/email) because you watch your own repo.
