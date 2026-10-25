# Crypto Flow Lab

**Research question:** does external crypto order flow improve forecasts of future
Kalshi binary-contract price changes, beyond prices, volatility and time to expiry?

The first assets are BTC and ETH; SOL is available as an explicit extension.
This milestone implements discovery, raw WebSocket capture and integrity checks.
It does not fit a forecasting model or claim an executable trading edge.

## Setup and offline verification

Use Python 3.10 or newer:

```bash
git clone https://github.com/avinazz3/kalshi-arb.git
cd kalshi-arb
python3 -m pip install -e '.[stream]'
python3 -m unittest discover -s tests -v
python3 -m kalshi_lab demo --db data/demo.sqlite
python3 -m kalshi_lab inspect --db data/demo.sqlite
```

The demo is explicitly synthetic. Expect two Kalshi frames, two Coinbase frames,
both snapshot types, zero hash failures and zero sequence discontinuities.
Choose a new demo path if that file already exists. Base fee/demo commands use
the standard library; streaming and the full test suite use the optional dependencies.
If you installed the old `kalshi-arbitrage-lab` distribution, uninstall it first
to avoid two installed distributions providing the same import/console command.

## First live setup test: public reference data

```bash
python3 -m kalshi_lab capture --reference-only --assets BTC ETH --seconds 30
python3 -m kalshi_lab inspect --out data/quality_report.json
```

This records Coinbase Advanced Trade heartbeats, full level-2 updates and batched
market trades. It needs no credentials. It cannot evaluate cross-venue prediction.
Full book snapshots can be large; inspect database growth before longer captures.

## Joint Kalshi / Coinbase pilot

Discover current contracts and archive their series, event and market metadata:

```bash
python3 -m kalshi_lab discover --assets BTC ETH
```

The output manifest retains rule text, settlement-source links, available fee
metadata and the authoritative exchange index. It does not approve a settlement
interpretation or infer an effective fee policy.

Kalshi requires authenticated WebSocket sessions even for public market data.
Configure credentials locally using the access needed for market-data subscriptions:

```bash
export KALSHI_API_KEY_ID="your-key-id"
export KALSHI_PRIVATE_KEY_PATH="/absolute/path/to/kalshi.key"
python3 -m kalshi_lab capture --seconds 60
python3 -m kalshi_lab inspect --out data/quality_report.json
```

Use credentials/access permitted for your account and location. Keep private keys
outside the repository. The collector never records auth headers or calls account,
funding or order-placement endpoints. Do not paste credentials into chat.

**This is a fixed-window pilot.** Joint capture stops at the requested duration
or two seconds before the earliest selected contract closes, whichever is sooner.
It does not rotate into the next 15-minute market. Rerun discovery before another
window. Coinbase-only mode can run longer (up to one hour per invocation).

## What the recorder preserves

- A session manifest with asset/contract mappings and capture configuration.
- Raw application-level WebSocket messages, byte hashes and connection IDs.
- One process's wall and monotonic receipt timestamps, taken before parsing or
  database queueing; source timestamps remain inside raw messages.
- Connection/disconnection events. Network failures reconnect with fresh IDs.
- A bounded background writer queue. A full queue aborts capture instead of
  silently dropping messages.

SQLite writes are batched, so a hard crash can lose the final uncommitted batch.
Graceful completion flushes the queue. Receipt times are application-level times,
not wire-arrival or exchange-engine timestamps; local buffering/CPU/network delays
remain part of the measurement.

The inspector checks hashes, malformed frames and sequence discontinuities.
It also records completion markers and per-venue first/last frame times, so
connection setup time is not mistaken for actual observation coverage.
Kalshi sequence checks are per subscription and connection; Coinbase sequences
span channels within a connection. Counts are not order-book validation, data
completeness certification, opportunity durations or trading results.

## Reusable execution-cost arithmetic

```bash
python3 -m kalshi_lab fees --input examples/fee_orders.json --balance-precision 0.0001
```

The example is a hypothetical purchase of eight YES contracts at $0.44 followed
by a sale at $0.52. Its explicit fee policy produces $0.2778 in modelled fees and
a $0.3622 net cash change. No such trade has been observed or executed.
See [the fee model](docs/FEE_MODEL.md) for rounding and configuration limits.

## What follows this milestone

Read [the experiment specification](docs/RESEARCH_SPEC.md) before collecting a
research sample. Next come snapshot/delta reconstruction, market-window rotation,
leakage-controlled feature/label construction, baseline forecasts and chronological
evaluation. Economic replay and supervised live execution follow only if warranted.

The earlier CPI arbitrage experiment, relation proposals and recorded CPI sample
were removed from the active tree. Git history retains that work. The distribution
is now `kalshi-crypto-flow`; the `kalshi_lab` import and `kalshi-lab` console command
remain stable. Existing old capture databases are not stream datasets.

GitHub Actions installs the streaming extra and runs offline tests, synthetic
capture/inspection and fee examples on Python 3.10 and 3.12. No live service or
credentials are required by CI. Official references are in [SOURCES.md](docs/SOURCES.md).
