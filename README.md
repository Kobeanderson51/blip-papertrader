# Paper Trader

Trades **fake money** ($50 start, $100 goal) across 27 Solana meme coins (screened Nov 18: $200k+ liquidity, 30+ days old, real volume; see config.json) against **live prices** (DexScreener, free, no key) and charges realistic costs (0.25% swap fee, network fee, slippage scaled to pool liquidity). The point is to find out whether a strategy makes money *after costs* before any real money is involved.

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

## Learning loop

Every closed trade gets a post-mortem in `data/journal.jsonl` (best/worst point while held, market context, what a different exit would have done, and a plain-English lesson). Rules never change automatically from single trades (that is curve-fitting). Instead, a weekly review groups repeated loss patterns, turns them into candidate rule changes, backtests them on the full history with the same honest method as `research/`, and proposes only those that hold up. Changes need the owner's approval.

## The desk

`desk.py` splits every run into six seats that share one rulebook (`desk_rules` in config.json): **Scout** (tradable coins, skips thin/stale pools) → **Chartist** (oversold-dip setups on closed 1h candles) → **Quant** (exact plan: entry, stop, size, exit) → **Risk Manager** (hard limits: positions, risk per trade, daily loss, losing-streak pause, kill switch) → **Executor** (paper trades approved plans only, never chases) and **Desk Lead** (daily brief at 7:30 AM ET in `data/brief.json`). Every handoff is logged to `data/desk_log.jsonl` as SEAT | TICKER | IDEA | EVIDENCE | NEXT STEP. The seats are code, not chat agents, so the rules are walls rather than requests; Claude reviews the desk with you in chat.

Replay of the test period: the desk's extra safety rules (3% daily loss stop, 3-loss pause) cut results from -1.4% to -4.7%, because they block buys right after drops, which is when a dip strategy buys. They are kept for survival; revisit in a weekly review.

## Nov 18: universe widened to 27 coins

Same rsi_dip rules, no re-tuning. Held-out test period (Aug 26 → Nov 18), 0.55%/side costs:
original 10 coins +0.5% (36 trades); 17 new coins +4.3% (53 trades, PF 1.71; +0.8% at 2x costs) while simply holding them lost 21.5%;
all 27 +2.9% (89 trades; -0.8% at 2x costs). Caveat: the new coins were -1.8% on the earlier (train) period, so the edge is small and inconsistent.

## Automatic coin scanner (every run)

`universe.py` (Scout) checks GeckoTerminal's trending and top-volume Solana pools every ~15 minutes and adds a coin only if it passes the same safety filters as the base 27: liquidity >= $200k, 24h volume >= $100k, market cap >= $2M, oldest pool >= 30 days, volume not wildly above market cap, quoted in SOL/USDC/USDT, and not a stablecoin, major, staked/wrapped token or tokenized stock. Up to 100 coins total. Auto-added coins (tagged NEW in the app) are dropped after failing the filters on 3 separate days, never while held. Pump.fun's own site is not scanned (it blocks automated access); coins that started there show up once they trade on DEXes, and brand-new launches are excluded on purpose.

## Stocks paper account (added Nov 18, 2026)

`stocks.py` runs a **separate $50 paper account** on 64 large US stocks and ETFs (started with 15 (SPY, QQQ, AAPL, MSFT, NVDA, TSLA, AMZN, META, GOOGL, AMD, NFLX, PLTR, COIN, JPM, XOM); 49 more added after the unchanged rule stayed positive on them too: +0.5% on unseen data over 54 trades, 57% won, still positive at 2.5x costs, 29 of 36 settings profitable). Fake money only; there is no live mode in this file.

- Rule: hourly RSI(14) on closed bars crosses below 15 → buy (1/6 of equity, max 6). Sell when RSI > 50, price −4% from entry, or after 48 market hours. Trades only while the US market is open. Data from Yahoo Finance.
- Why this one: the Nov 18 test (2 years hourly, tuned on first 60%, checked on last 40%, 216 variants counted) — stock dip buying made +2.8% on unseen data over 17 trades, 86% of settings profitable, still positive at 2.5× costs. Buy-and-hold made +11.2% over the same stretch, so it is not proven better than holding. 17 trades is a small sample.
- Same kill rules (20% drawdown, or no profit after 30 trades) and the same 50-trade test. Results on the dashboard's **Stocks** tab.
- Also tested and rejected the same day: currencies (USD/JPY, EUR/USD, GBP/USD, AUD/USD, USD/CAD; every strategy lost on unseen data) and crypto majors (BTC, ETH, SOL, XRP, DOGE; nothing survived higher costs).
