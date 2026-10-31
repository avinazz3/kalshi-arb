# Multi-day evaluation and receipt-logged observation

Version 0.9 adds `compare`, `batch`, `shadow` and `paper`. The rolling/EWMA GBM
baseline, settlement rule and historical forecasting defaults stay fixed.
No command submits orders or accesses account balances or positions.

## Compare the two runs already completed

```bash
python -m kalshi_lab compare --analysis-dirs data/analysis/btc-day-001 data/analysis/btc-day-002 --out-dir data/comparisons/first-two-days
```

Each analysis report's configuration and artifact hashes are checked, and the
scores are recomputed from its rows. Different forecast settings, duplicate
contracts and overlapping observed contract intervals are rejected. Underlying
warm-up observations may overlap; they are not duplicate evaluation contracts.
Source exclusions remain visible. A completed but partial run is not silently
treated as a full day. These integrity checks do not authenticate the original
exchange data or certify historical availability.

Outputs are `daily_scores.csv`, pooled `rows.csv`, `calibration.csv`, `summary.md`
and `report.json`. Dates use the UTC **contract opening day**. Pooling weights
individual matched forecasts, rather than giving short/partial runs the same
weight as complete days. Calibration, loss concentration and feature panels are
recomputed on pooled common rows. Each horizon remains a separate scoring panel.

Paired excess Brier/log loss is model loss minus market loss on the same forecast.
For exploratory uncertainty, `compare` resamples non-circular contiguous blocks
of UTC days. Every forecast and horizon inside each selected day travels together.
Default block length is two days, with 2,000 resamples and seed 314159. Percentile
95% intervals are recorded in `report.json`. Use `--block-days 1`, `2`, or `3` to
inspect sensitivity when there are enough dates. Missing calendar dates are never
bridged inside a block.

Intervals are **unavailable** below max(8, 4*block_days) observed dates or four
candidate blocks. This is an explicit engineering floor, not proof that eight
days are statistically sufficient. Block length can miss longer dependence;
partial coverage, date selection and repeated model selection remain concerns.
The sampler preserves the pairing; it does not treat three horizons of one
contract as three independent outcomes. It keeps saved Monte Carlo probabilities
fixed and does not estimate model-parameter or Monte Carlo uncertainty separately.

Declare development/validation/test date ranges outside the tool before tuning.
The tool never labels inspected data as held out automatically.

## Run additional days and reuse completed work

```bash
python -m kalshi_lab batch --start 2026-10-25T00:00:00Z --days 4 --root-dir data --out-dir data/batches/first-four-days
```

This includes October 25–28. Existing `btc-day-001`/`btc-day-002` history directories
are recognized by their manifest interval, and matching forecast/analysis directory
names are reused. No fresh download, signing credentials or Monte Carlo work is
needed for a fully completed, verified day. More than one legacy history match is
an error. Newly created runs use `btc-YYYYMMDD`.

For each missing day the workflow sequentially downloads history, reconstructs
settlement, runs fixed-model forecasts and produces diagnostics. The existing
downloader's bounded retry/resume checks apply to interrupted downloads. Each
24-hour interval must end at least 75 minutes before the command starts. Starts
must be UTC midnight; the batch is limited to 30 days. Default paths are 2,000;
`--paths` is available for explicitly separate experiments, and incompatible saved
settings are refused.

Outputs under `--out-dir` include `batch.json` and `comparison/`. Existing batch
outputs are preserved. On failure or interruption, use a **new** batch output path
with the same start/days/root; verified completed work is reused. A partly written
forecast or analysis is not overwritten or silently resumed: retain it and select
a clean root or move the failed stage aside explicitly. The batch stops on the
first failure instead of hiding gaps. Existing audit reports are preserved, while
the current data is always audited again before use.

## Record live observations and forecasts

Install the existing data dependency and provide your local credentials through
`KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`, as for history downloads:

```bash
python -m pip install -e '.[data]'
python -m kalshi_lab shadow --run-dir data/shadow/btc-live-001 --minutes 120
```

The recorder is a bounded foreground process. It does not set a scheduled task.
Use a separate terminal if you also run a historical batch. It follows the active
KXBTC15M contract through rollovers, streams BRTI on an independent background
thread and queries up to ten bid levels per YES/NO side using existing REST code.
`websocket-client` handles framing, ping/pong and reconnects. The sole WebSocket
subscription is `cfbenchmarks_value` for BRTI; REST uses allowlisted GET endpoints.
Credential identifiers, key paths, authentication
headers, raw HTTP bodies and upstream error messages are not written.

Default cycle interval is 0.5 seconds, compensating for time spent requesting and
processing data. A cycle runs more slowly if requests take longer. `--interval`
accepts 0.5–10 seconds. Requests are sequential with a 1–30 second socket timeout;
there is no per-poll retry loop. Slow book requests do not block index ingestion.
Both wall-clock and monotonic deadlines stop new requests; one in-flight request
can finish late. Laptop sleep cannot be prevented by Python: on macOS run with
`caffeinate -i python -m kalshi_lab shadow ...` and keep the lid open. Long requests
and collection pauses are reported; retained input history is cleared after a pause.
Output, the stream queue and request counts are bounded. Ctrl-C flushes artifacts
and checkpoints an interrupted report; use a new directory to restart.

`observations.jsonl` records normalized index points, market metadata, bid ladders
and structured failures with request-start and local receipt times, response
hashes and observation IDs. Order books contain YES/NO bids; implied asks use
1 minus the opposite-side bid. Array ordering is normalized, duplicates/crossed
books are rejected, and no integer-cent/fixed-point unit guessing occurs.
`decisions.jsonl` logs a forecast or an explicit unavailability reason at each
observed contract's nominal 10/5/2-minute decision.

The default is live-only collection with at least one hour of warm-up. First run
`python -m kalshi_lab shadow --run-dir data/shadow/check-001 --minutes 2` and inspect
`report.json`: `stream_samples` must increase and `coverage` lists received versus
61 required minute endpoints, missing timestamps, index age and readiness. This
short check normally produces zero forecasts and exits 2. No stream ticks within
30 seconds fails early instead of spending hours collecting only books.

Optional `--bootstrap-history` makes three bounded CF hour requests at startup.
These points get their **actual current receipt time**, not their old publication
time. Forecasts only use points received by calculation start. Current history
may lag, so initial forecasts can lack endpoints until live observations cover
the gap. It does not guarantee immediate forecasts. Reconnects and upstream gaps
can still leave missing exact endpoints. No gap is silently filled.

Stream frames retain their socket receipt time in `stream_received_at`. For
compatibility with existing paper replay, `received_at` is conservatively the
later time the main loop drains and records the tick, before forecast calculation.
Only raw upstream values are used; the feed's supplied average fields do not
replace the audited settlement calculation. Stream errors are sanitized.

At a due decision, the latest whole-second source strictly before calculation
start must be no more than three seconds old. All 61 exact minute-spaced input
points must have arrived before calculation. Missing, amended or repeat-flagged
inputs are unavailable; there is no interpolation or forward filling. Frozen
input values, observation IDs, receipts and a digest are saved with the forecast.
The model uses the same volatility estimates and GBM transitions as the historical
baseline, with actual source/decision timestamps. It does not pretend a delayed
live forecast occurred exactly at the nominal retrospective horizon.

Forecasts start no more than ten seconds late. A book must be two-sided and no
more than five seconds old by calculation completion. A forecast reaching the
final averaging minute is unavailable. Reference/rule changes suppress subsequent
forecasts for that contract and remain recorded. Past missed decisions are logged
once as missed rather than fabricated from newly fetched data.

`report.json` records readiness, counts, errors, exclusions and artifact hashes.
Exit 0 means the recording finished with forecasts and no acquisition or decision
gaps; exit 2 includes warm-up-only runs and zero forecasts; Ctrl-C exits 130.
`wall_elapsed_seconds` reports actual wall duration. Local receipt evidence is useful,
but the tool retains `point_in_time_certified=false`: it cannot certify clocks,
server caching, publication timestamps or the exchange's true book-update time.

## Evaluate hypothetical trades after labels become available

After the recording ends, download/audit the historical UTC day(s) containing its
contracts with the existing workflow. Use those datasets only for outcome labels:

```bash
python -m kalshi_lab paper --shadow-dir data/shadow/btc-live-001 --run-dir data/history/btc-20261029 --policy examples/paper_policy.json --out-dir data/paper/btc-live-001
```

Repeat `--run-dir` for multiple label days. Labels must pass settlement
reconciliation. Observed and settled reference/rules/schedules must agree. A
recorded contract absent from the label datasets remains pending, rather than
being dropped from attempted-position counts or assigned an outcome.

`examples/paper_policy.json` contains **illustrative assumptions**, not discovered
or certified exchange fees: quadratic taker multiplier 1, maker multiplier 0,
balance precision $0.0001, quantity 1, minimum expected net edge $0.03 per contract,
1,000 ms latency, 5,000 ms maximum snapshot wait, and $0.01 allowed price slippage.
Review and copy the file before using different assumptions. Every field is
required; the evaluator never silently substitutes a current fee schedule for
historical policy. It uses the repository's existing per-order fee/rounding logic.

For each model independently, the evaluator walks the **decision-time** ask depth
and computes expected payout less cost and modeled fees for YES and NO. It picks
the eligible side with the larger expected surplus. Only the first eligible
attempt per contract/model is allowed, including failed attempts. No parameter
or threshold search is performed. There is no combined rolling-plus-EWMA portfolio.

Execution uses the first recorded book whose **request begins after** forecast
availability plus latency, and whose receipt is within the configured maximum
wait and before contract close. A snapshot whose request began before eligibility
is rejected even if its response arrived later. The limit is the worst
decision-time ask needed for the chosen quantity plus configured slippage.
The scenario assumes fill-or-kill: the full quantity must fit inside the saved
depth and price limit, otherwise it is unfilled. It never extrapolates deeper
liquidity, reuses the original snapshot as a later fill, or invents missing books.
Settled fills are held to their $1/$0 payout. Pending fills have no realized P&L.

`attempts.csv` (when attempts exist) and `report.json` preserve signal expectations,
assumed fills, failure reasons, costs, fees and net P&L separately by model. These
are snapshot scenarios: REST depth does not guarantee that an order would fill.
Queue competition, cancellation between snapshots, routing, market impact, actual
order acceptance and balances are not modeled. Results do not establish live
profitability. Build a larger fixed-model dataset before choosing a model extension.
