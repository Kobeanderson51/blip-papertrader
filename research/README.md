# Strategy research (Nov 17, 2026)

Hourly candles for 10 Solana meme coins, Apr 23 → Nov 17 2026 (GeckoTerminal free tier; ~208 days is its history limit).
`python3 fetch.py` re-downloads, `python3 backtest.py` re-runs and writes `results.json`.

Method: signals on bar close, fills at next bar open (no look-ahead); 0.55% cost per side (0.25% fee + 0.30% slippage estimate), stress test at 2x; train = first 60% of time (to Aug 26), test = last 40%, never random; 240 parameter variants tried in total, all counted.

Known bias: the 10 coins were chosen because they still trade today (survivorship bias), which flatters every strategy.
