# Cross-venue order flow and crypto binary price discovery

## Question and scope

Does external order flow provide incremental information about future Kalshi
crypto-binary price changes, after controlling for existing price information,
and does any predictive improvement survive realistic execution costs and delay?

BTC is the reference experiment; ETH is the first extension. SOL follows after
the data pipeline passes the same checks. Primary targets are midpoint changes
over 1 and 5 seconds. These are quote-movement targets, not settlement labels.

OpenMarket (Young, arXiv:2607.26245) motivates the study. Its settlement forecasting
experiment did not beat the market prior out of sample, and its costed trading
simulation was negative. Applying related ideas to Kalshi, additional assets and
future quote movements is a proposed extension, not a demonstrated alpha source.

## Implemented milestone

Explicit crypto-series discovery, raw shared-clock WebSocket capture, integrity
reports, exact snapshot/delta reconstruction and as-of feature/label datasets.
Raw Kalshi/Coinbase frames remain preserved. The offline layer adds quote history,
best-level queue imbalance, message-level Coinbase OFI, signed aggressive trade
notional and 1s/5s future midpoint-change targets. See [DATASET.md](DATASET.md) for
precise availability, validity and audit rules. There is no estimator or trading
backtest in this milestone.

Joint capture selects one fixed window per asset and stops before expiry. Automatic
rollover is a next milestone. A short setup pilot verifies transport only; it does
not supply a research-quality training/test sample.

## Protocol details that must survive reconstruction

Kalshi snapshots have YES and NO bid ladders; implied asks come from the opposite
outcome. Dollar and quantity fields are fixed-point strings. Deltas change level
size; snapshots replace state. Detect subscription sequence gaps and invalidate
state until a new snapshot. Archive the exchange index; do not assume crypto is
on shard zero.

Coinbase level2 is subscribed by that name but arrives on l2_data. new_quantity
is the new absolute quantity, not an additive change. Coinbase sequences are
per connection, across channels. market_trades batches arrivals over roughly
250 ms. Its side field is the maker's side: aggressive direction is opposite.
Snapshot historical trades must not be miscounted as newly arriving flow.

Spot prices from Coinbase are reference observations, not Kalshi's settlement
oracle. Review each supported series' actual index, start/end averaging windows,
rounding and equality conditions. Do not substitute instantaneous spot prices
into a digital-option model of an averaged-index payoff.

## Planned information set and labels

Use the common monotonic application receipt clock within one capture session.
At decision time t, features may use only frames received at or before t.
Do not pair with the nearest external quote if it arrived later. Venue source
timestamps are diagnostic; combining clocks naively can create artificial leads.

On a fixed decision grid, reconstruct valid Kalshi midpoint m(t). The target is
m(t+h)-m(t) for h=1s and 5s. Labels use valid state at t+h; they are never input
features. Reject rows crossing expiry, disconnections, missing snapshots or
unreconciled sequence gaps. Treat quiet unchanged books differently from an
unhealthy/disconnected feed. No imputation of missing labels as zero returns.

Features to pre-specify:
- Contract history, spread/depth, time left and verified strike/reference distance.
- Underlying returns and volatility, including market-wide BTC returns.
- Signed aggressive trade notional, queue imbalance and book-event order-flow
  imbalance as distinct feature groups.
- Each feature's lookback and availability rule, including delayed batched trades.

The current dataset implements receipt-time alignment, contract-window bounds,
continuity checks and the core own-asset flow features. Volatility, verified
strike/reference distance, cross-asset features and full M1/M2 comparisons remain
to be implemented. The existing feature set is not the full price-only baseline.

## Planned model comparison

M0 predicts no change. M1 uses contract history, underlying prices, volatility,
strike distance and expiry. M2 adds own-asset external order-flow features.
M3 adds cross-asset flow and compares separate-asset versus pooled/partial-pooling
models. Incremental gains of M2 over M1 are the main hypothesis, rather than
simply rediscovering that binary prices respond to their underlying.

Start with transparent regularised linear/logistic models. More flexible models
must beat the same baselines under identical chronological splits. Model choice,
horizons and asset comparisons must be recorded before looking at the final test.

## Evaluation and falsification

Use chronological train/validation/test periods, grouping full contract windows
and synchronising calendar splits across assets. Purge overlapping labels and
apply a buffer accounting for feature lookbacks. Resample at day/window blocks,
not individual high-frequency rows. The final test period stays untouched during
feature and threshold selection. Many messages are not many independent bets.

Report forecast losses relative to M0/M1 with uncertainty and asset/regime
breakdowns. Report data gaps, actual observation duration and selection effects.
Transfer from BTC to ETH/SOL is a separate empirical test; asset-specific fits
must not be presented as proof of transfer.

For economics, replay available entry asks and exit bids at plausible order-arrival
times, apply both-side fees, depth, partial fills and exposure limits. A predicted
midpoint gain is not an executable profit. Do not invent atomic fills or reuse
the same historical depth repeatedly. Small-asset slowness may be offset by spreads.

Success means a reproducible, well-controlled answer. A null predictive result or
negative net P&L is valid evidence and should stop unsupported trading claims.
