# Historical BTC dataset and settlement audit

This milestone downloads a bounded retrospective sample, then reconstructs
settlement inputs under explicit hypotheses. It does **not** estimate volatility,
fit a model, score predictions or place trades. Do this audit before building
rolling/EWMA probabilities. The official outcome and published opening reference
are the targets for this audit, not evidence of forecasting skill.

## Run a small sample first

After updating your checkout and activating your virtual environment:

```bash
python -m pip install -e '.[data]'
python tests/run_checks.py
python -m kalshi_lab history --start 2026-10-25T00:00:00Z --hours 1 --run-dir data/history/btc-hour-001
python -m kalshi_lab audit --run-dir data/history/btc-hour-001
```

Use local `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`, as for `diagnose`.
Choose a **whole UTC hour**. An explicit offset is accepted if the instant aligns
to a UTC hour. The contract interval is `[start, start + hours)` by **opening
time**. `--hours` is 1–24 (default 24); `--warmup-hours` is 1–24 (default 1).
The extra history is reserved for subsequent past-only volatility estimation;
this milestone does not calculate returns or fit volatility.

The final contract closes at `end`. To audit a candidate that includes the
close tick, we also fetch the **hour beginning at end**. The requested end must
be at least **75 minutes old**, allowing that whole hour to finish plus the
upstream documented possible 15-minute publication delay. This is deliberately
conservative and is not a guarantee that data will exist.

After checking the small sample, use a new directory for one day:

```bash
python -m kalshi_lab history --start 2026-10-25T00:00:00Z --hours 24 --run-dir data/history/btc-day-001
python -m kalshi_lab audit --run-dir data/history/btc-day-001
```

These are historical dates, not a rolling schedule. Select another sufficiently
old interval if appropriate. Commands finish after downloading; they do not run
for the requested number of hours. The normal one-day case is 96 contracts,
96 candle requests, 26 index-hour requests and one recent-market listing request
(123 requests total). Archive fallback and pagination can increase this to 130
with default warm-up. The absolute configured maximum is 153 requests with
24 hours of warm-up. Requests are sequential with a 0.3-second minimum pause
between calls; there are no retries, parallel sockets or order/account calls.
Each HTTP response is capped at 8 MiB. Timeout is per socket operation, not a
whole-run deadline. A 429 or any failed request stops with a checkpoint; rerun
in a new directory after investigating. Resuming is not implemented.

## Files and provenance

| File | Contents |
| --- | --- |
| `manifest.json` | Interval, request paths/parameters, receipt times, response hashes, artifact hashes, coverage issues and final status |
| `contracts.json` | Frozen ticker, UTC open/close, published reference, comparison, full primary/secondary rules, result and optional numeric expiration value |
| `quotes.json` | One-minute bid/ask closing prices and validity flags, keyed by ticker and candle end time |
| `index/YYYYMMDDTHH.csv.gz` | Every returned typed BRTI sample, preserving millisecond timestamps, exact decimal values and revision/repeat flags |
| `settlement_audit.json` | Offline reconstruction, gaps and match/mismatch counts for each hypothesis |

Compressed CSV retains the original returned frequency (up to 18,000 points/hour),
so a 5 Hz history is not destructively collapsed to a one-second series. Only
whole-second observations are used by the present audit. The one-hour sample
fetches three hours including warm-up and final-close coverage; a default day
fetches 26. There are no full order books or repeated websocket snapshots.
Actual compressed size depends on values and flags; inspect each artifact's
`bytes` in the manifest instead of assuming a compression ratio.

Raw HTTP bodies, auth headers, key identifiers and key paths are not persisted.
Response hashes identify the received bodies; normalized artifact hashes detect
later accidental edits. Hashes do not independently prove historical truth or
provide cryptographic source attestation. Existing directories and audit outputs
are refused. Interrupted downloads retain their manifest and completed artifacts.

Market discovery checks the recent endpoint with close-time filters **without**
`status=settled` (that combination is unsupported). Results are checked locally.
Missing expected quarter-hour openings trigger a bounded archive search by series;
the archive endpoint has no time filters. Each listing is capped at four pages of
1,000 markets. A page cap, cursor loop, conflicting snapshot, unsupported/provisional
outcome or duplicate window is explicitly blocked. Missing quarter-hour markets
are reported, not invented. An unlisted interval could be legitimate; investigate.

Candles ending exactly at opening are excluded because they describe the previous
minute. Missing/null/crossed quotes are retained as invalid or reported missing;
prices are never carried forward. Candle close prices describe aggregated history,
with no executable size, original receipt timestamp or guarantee of tradable fills.

## What the audit tests

The full report has four fixed cases, with **no automatic winner**:

| Boundary hypothesis | 60 whole-second samples at endpoint T |
| --- | --- |
| `right_closed` | T−59s, T−58s, …, T; window `(T−60s,T]` |
| `left_closed` | T−60s, T−59s, …, T−1s; window `[T−60s,T)` |

Each is tested with `half_up` and `half_even` rounding of the **arithmetic mean
once to two decimals**. Both hypotheses assume timestamps exactly divisible by
1,000 milliseconds. A 5 Hz minute is **not** averaged as 300 observations. We do
not resample to nearest ticks, interpolate, forward-fill, or choose a sampling
phase that maximizes matches. If exact-second observations are missing, the mean
and its comparison are unavailable. Other publication phases require a reviewed
extension, rather than silent data repair.

For each contract and case the audit reports:

1. The reconstructed opening average versus Kalshi's published reference.
2. The reconstructed closing average, compared with that **published reference**
   using its `greater` or `greater_or_equal` field, versus the published YES/NO result.
3. Closing average versus the API's numeric `expiration_value`, when supplied.
4. Missing samples and amendment/repeat flags in each averaging window.

The third check is additional evidence; absence of an expiration value is counted
as unavailable, not a match. A correct binary outcome alone is weak evidence:
very different closing prices may still fall on the same side of the threshold.
Opening references and numeric closing values are stronger reconstruction checks.
No tolerance is used for cent-rounded price comparisons.

The audit also verifies artifact hashes, UTC schedules, bounded row counts,
strictly ascending index timestamps, exact-second coverage across the downloaded
hours, and missing contract openings. It can run offline from frozen files. To
write a second report, use a new `--out` path:

```bash
python -m kalshi_lab audit --run-dir data/history/btc-day-001 --out data/history/btc-day-001/audit-review-002.json
```

Exit **0** for `history` means the expected contracts, all 15 two-sided candle
closes per contract and all 3,600 exact-second ticks per downloaded hour were
present. Exit **0** for `audit` means computations finished with those coverage
checks satisfied. **It does not mean all reconstructions match.** Read the
`mismatches` and `unavailable` counts. Exit **2** means blocked/incomplete input;
exit **1** is an invalid invocation, failed integrity check or local error;
Ctrl-C exits **130**. The audit never sets `rule_review_required` to false.

## Remaining rule and availability questions

Kalshi's CF feed documentation describes the quarter-hour convenience accumulator
as `(T−60s,T]`. General CRYPTO terms describe a 60-second underlying average and
exclude revisions made after expiration. These are useful references, but the
applicable KXBTC15M market rules and settlement practice still control. Our current
rule snapshot says nearest two decimal places, which alone does not resolve an
exact half-cent tie. Whole-second sampling phase, precise boundaries and tie rule
remain review items. This command is a falsifiable reconstruction experiment,
not contractual certification. Do not select conventions just to improve fit.

Retrospective CF history can contain revisions; even no amendment flags cannot
prove it matches the values available to traders at past decision times. Original
point-in-time receipt and opening-reference publication times are not established
by these downloads. Consequently `point_in_time_certified` is always false.
Changing contract rules are preserved verbatim for review, not interpreted with an
NLP classifier. Mismatches may reflect boundaries, rounding, sampling, revisions
or a different settlement procedure, and should stop forecasting claims until
explained.

Once this audit has been reviewed on real downloaded data, the next milestone is
past-only rolling/EWMA volatility and probabilities at 10, 5 and 2 minutes before
close. One day first verifies machinery; broader regimes and an untouched future
sample are required for model-performance claims.
