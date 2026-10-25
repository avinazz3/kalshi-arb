# From raw messages to a research dataset

Version 0.3 adds offline reconstruction and feature/label construction. Run it on
one **completed joint capture session**. It does not fit a model, estimate profit,
place orders or rotate the recorder into another contract window.

## Verify the implementation without credentials

```bash
python3 -m unittest discover -s tests -v
python3 -m kalshi_lab research-demo --db data/research_demo.sqlite
python3 -m kalshi_lab replay --db data/research_demo.sqlite
python3 -m kalshi_lab dataset --db data/research_demo.sqlite --out data/research.jsonl
```

Choose unused output paths: demo creation and dataset export refuse to overwrite
existing files. The earlier `demo` command remains a tiny transport fixture;
use `research-demo` for this experiment. Its prices and events are invented.

The research fixture has 12 seconds of synchronized observations. With the default
settings it produces seven accepted rows and one rejected initial row. Verify:

| Decision time after fixture start | Signed aggressive notional over the previous second | Coinbase message OFI | Kalshi midpoint | Next 1s midpoint change |
| --- | --- | --- | --- | --- |
| 1s | $0 | 0 BTC | $0.495 | $0 |
| 2s | +$25.01 | +2 BTC | $0.495 | +$0.005 |

At 1.5s an aggressive buyer trades 0.25 units at $100.04. Coinbase reports maker
side `SELL`, so the signed notional is positive. The bid quantity changes from
1 to 3 units at the same price, so OFI is +2, not +3. A large historical trade in
the initial snapshot contributes no new flow. At 2.5s Kalshi's best NO bid is
removed: its implied YES ask rises from $0.50 to $0.51, lifting the midpoint by
$0.005. The trade and quote change cannot enter the 1s decision's inputs.

## Run on a captured session

```bash
python3 -m kalshi_lab replay --db data/capture.sqlite --out data/replay_report.json
python3 -m kalshi_lab dataset --db data/capture.sqlite --out data/research.jsonl
```

If the database contains multiple sessions, supply `--session SESSION_ID` to both
commands. Session clocks are not assumed comparable; the builder never joins
them. The rollover report compares frame gaps within one process/run only.
Reference-only captures can be replayed but cannot generate this
cross-venue dataset. Inspect zero-row exports and rejection counts before doing
any modelling. An accepted row is not certification that a sample is representative.

Defaults are a 1000ms decision grid, 1000ms lookback, 1000/5000ms label horizons
and a 5000ms maximum feed silence. Override with `--grid-ms`, `--lookback-ms`,
`--horizons-ms` and `--max-silence-ms`. Pre-specify these settings before evaluating
predictions. Finer grids create overlapping observations, not independent bets.

## Book reconstruction and validity

All price/quantity arithmetic uses Decimal. Kalshi YES bid price is the maximum
YES bid. YES ask is one minus the maximum NO bid, with the corresponding NO size.
Snapshots replace complete ladders; deltas add to a level quantity. Coinbase
`new_quantity` replaces a level's absolute size; zero removes it. The best bid is
the highest bid and the best ask the lowest offer.

Two-sided non-crossed books are required. Locked quotes are retained. Empty or
crossed books, negative resulting sizes and malformed frames invalidate state.
Recovery requires a fresh valid snapshot. This conservative policy may reject
otherwise repairable data; it never silently repairs a broken book.

Kalshi sequence continuity is per connection and subscription ID. A gap invalidates
all known markets on that subscription. Coinbase sequencing is connection-wide,
including heartbeats and subscription acknowledgements; a gap invalidates all its
product books and trade-flow coverage. Duplicate or backwards sequence messages
are not applied. Reconnects and subscription removal invalidate affected state.
Snapshots recover one book at a time, not every market on a shared subscription.

Each invalidation and snapshot replacement creates a new book epoch. A feature
lookback or label interval must stay in one valid epoch, even if its endpoints
look valid after an intermediate gap and recovery. Unknown trade coverage is not
treated as zero flow. A trade snapshot or subscription acknowledgement establishes
coverage; historical snapshot trades are excluded. Trade IDs are deduplicated per
product across the session, and conflicting duplicate contents invalidate the feed.

Liveness and quote age are separate. An unchanged book remains usable while
other messages on the same venue connection arrive. A silence longer than the
configured limit rejects any interval spanning it, including a silence that ended
before the label endpoint. This is a conservative activity proxy: the original
recorder does not archive protocol ping/pong acknowledgements. A quiet Kalshi
connection can therefore lose usable rows despite still being connected. Bounds
on silence do not establish perfect feed health or cross-venue synchronization.
Sample tails stop at the last observed frame of the quieter venue.

## Information timing and expiry

Feature state at decision time t uses only messages with application receipt
time <= t on the common monotonic clock. Equal-time messages are processed in
capture ID order, and state after all such messages is used. Flow windows are
(t-L, t]. No nearest-future quote matching or venue-source timestamps are used.
Coinbase batched trade data becomes available at receipt, not the trade's source
timestamp. Local buffering and network latency remain measurement limitations.

Each target is m(t+h)-m(t), where m is reconstructed Kalshi YES midpoint. It uses
the latest valid state at or before t+h, rather than the first quote after t+h.
Unchanged healthy quotes produce a legitimate zero change; missing labels do not.
Every requested horizon must be valid for a row to be accepted. Both venue books
and liveness must stay valid through the label interval; this availability filter
must be reported when interpreting results.

For expiry, wall time is anchored once to the first session receipt and advanced
using monotonic elapsed time. Sessions with observed wall/monotonic deviation over
250ms fail dataset construction. Feature lookbacks must begin at or after market
open, and every label must end strictly before market close. This detects wall
clock jumps, not an absolute clock offset: use a synchronized collection host.
Coinbase spot remains a reference, not Kalshi's settlement index.

## Feature definitions

The implemented feature set is deliberately inspectable:

| Field | Definition / units |
| --- | --- |
| `kalshi_midpoint`, `kalshi_spread` | (bid+ask)/2 and ask-bid; dollars per YES share |
| `kalshi_bid_size`, `kalshi_ask_size` | Best-level quantities; contracts |
| `kalshi_queue_imbalance` | (bid size-ask size)/(bid size+ask size) |
| `kalshi_midpoint_change` | m(t)-m(t-L); dollars per share |
| `coinbase_midpoint`, `coinbase_spread` | Spot quote values; USD per base unit |
| `coinbase_return` | spot_mid(t)/spot_mid(t-L)-1; simple return |
| `coinbase_queue_imbalance` | Best-level normalized size imbalance |
| `coinbase_message_ofi` | Sum of best-level OFI across L2 event batches in (t-L,t]; base units |
| `signed_aggressive_notional_usd` | Aggressive-buy notional minus aggressive-sell notional in (t-L,t] |
| `total_trade_notional_usd`, `trade_count` | Unsigned notional and count of newly received, deduplicated trades |
| `seconds_to_close` | Anchored time until the recorded contract close |

For previous/current best bid prices b and sizes qB, and ask prices a and sizes qA:

```text
e = 1[b_new >= b_old] qB_new - 1[b_new <= b_old] qB_old
  - 1[a_new <= a_old] qA_new + 1[a_new >= a_old] qA_old
```

Compute this after each complete Coinbase L2 event batch for a product, never
across snapshots, gaps or connections. It is Cont-style **message-level** OFI;
batched level2 cannot recover individual matching-engine events. OFI and signed
trade notional are different measurements, and raw OFI scales differ by asset.

Volatility, verified strike distance, longer lookbacks and cross-asset predictors
are not implemented yet. These features are not a complete price-only M1 control
set from the research specification. Do not claim incremental order-flow alpha
from a comparison that omits those controls.

## Export and audit

JSONL rows separate `features` from future `targets`. Decimal values are strings
to preserve exact decimal inputs; convert them deliberately in the modelling layer.
IDs and timestamps are integers. Each row includes asset/contract/product keys,
decision time, feature window start, last available frame receipt/ID and label
end times. These provenance fields are metadata, not model predictors.

The `.jsonl.manifest.json` sidecar records version, schema, settings, synthetic
status, accepted/rejected counts, reconstruction diagnostics, clock checks and
the JSONL SHA-256. `source_digest` hashes the selected session's configuration and
ordered event identity, connection, clocks and verified body hashes. It is stable
across repeated reads of unchanged data. Hashes detect changes against recorded
values; they do not authenticate the exchange or prevent a deliberate rewrite.
Replay holds a SQLite read snapshot. Dataset export requires a finish marker;
use a stopped capture to ensure a reproducible input.

Rejected-row counts record the first failing check per candidate, not every
possible defect. Initial warmup can appear as `outside_contract_window`,
`feed_silence`, `invalid_book` or `trade_flow_unavailable`. Empty exports explicitly
report zero accepted rows. Inputs and existing dataset outputs are preserved.

## Next experiment

Collect multiple complete contract windows and inspect reconstruction/coverage
before building chronological splits. The [collection coordinator](COLLECTION.md)
handles window rotation with explicit gaps and separate sessions. Then add the missing price controls and compare transparent
regularized forecasts against no-change and price-only baselines, with purged
calendar splits and block-level uncertainty. A costed executable-price replay
comes after predictive evaluation. This dataset alone is evidence about neither
profitability nor tradable latency advantages.
