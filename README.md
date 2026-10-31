# Kalshi Bitcoin Probability Lab

**Research question:** can a settlement-aware model of Bitcoin prices and
volatility improve forecasts of BTC 15-minute contract outcomes relative to
Kalshi's quoted probabilities?

The current milestone provides an offline digital-option benchmark, a small
read-only access diagnostic, and bounded historical BTC downloads with an offline
settlement reconstruction audit. The model takes explicit inputs; it does not
estimate volatility, fit a forecasting model, place orders or report trading
performance. The example is synthetic. See the
[research specification](docs/RESEARCH_SPEC.md) and
[model mathematics and verification](docs/DIGITAL_MODEL.md).

## Install and verify

Use Python 3.10 or newer. From your existing checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[data]'
python tests/run_checks.py
```

If your virtual environment already exists, activate it and reinstall instead of
creating it again. The digital benchmark uses the standard library. The optional
`data` extra installs cryptography for authentication and websocket-client for
the live BRTI feed. Keep the distribution name
`kalshi-crypto-flow` for
installation continuity; the import `kalshi_lab` and command `kalshi-lab` are stable.

## First: run the offline probability benchmark

```bash
python -m kalshi_lab digital --input examples/digital_contract.json
```

The invented example has index value $100,100, reference $100,000, 600 seconds left,
60% annual volatility and zero annual price drift. With its explicitly supplied
rounding rule, the terminal-price analytical probability is about **0.648257**.
The command also reports independent-path simulations of the terminal and the
arithmetic-average payoffs, their Monte Carlo standard errors and Wilson intervals.
These intervals do not account for wrong model assumptions or estimated parameters.

The 60 averaging observations in this example are explicitly at offsets 541 through
600 seconds. This is a numerical example, not an approved interpretation of a live
contract. Live sampling boundaries, reference availability, rounding and comparison
must be verified from the applicable contract rules before using real observations.

To save a report, choose a new file:

```bash
python -m kalshi_lab digital --input examples/digital_contract.json --out data/digital-example.json
```

## Next: check access to the settlement index

Use your existing local `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` variables.
Private keys stay outside the repository. Do not paste them into chat.

```bash
python -m kalshi_lab diagnose --run-dir data/diagnostics/btc-001
```

The command checks a small sample of active BTC contract metadata/current quotes,
settled outcomes/historical quote candles, the current BRTI value and one historical
BRTI hour. It makes at most seven allowlisted GET requests, with no retries,
WebSockets, account endpoints or order endpoints. Each response is capped at 8 MiB;
`--timeout 10` is a socket timeout per request, not a total wall-clock deadline.

It writes `diagnostic.json` and, when available, typed `index_current.csv` and
`index_history.csv` samples. Existing run directories are preserved. Authentication
headers, private-key paths, raw response bodies and exception text are not saved.
The history request uses an hour starting two UTC hours before the current hour,
to avoid confusing publication delays with missing access. It is not a bulk download.

Exit code **0** means the sampled access checks passed. Exit code **2** means at
least one required check is blocked; the report identifies it. Exit code **1**
means an invalid invocation or local failure. A successful access sample does not
certify historical coverage, real-time availability, correct rule interpretation,
or a useful forecasting model. In particular, retrospective index history may be
revised. CF REST access requires the appropriate Kalshi account entitlement.

The diagnostic's public metadata calls still run if credentials are absent or
invalid; index checks report `credentials_unavailable`. HTTP 401/403 requires
checking account credentials/access. HTTP 503 can represent an upstream access
failure or an upstream service problem; do not infer entitlement from it alone.
Unknown/empty schemas and stale current index values are explicitly blocked.

## After access passes: download and audit a historical sample

```bash
python -m kalshi_lab history --start 2026-10-25T00:00:00Z --hours 1 --run-dir data/history/btc-hour-001
python -m kalshi_lab audit --run-dir data/history/btc-hour-001
```

This finishes after downloading historical data; it does not collect live for an
hour. The sample includes a prior warm-up hour and the hour containing the final
close tick. Start with one hour, then use `--hours 24` and a new directory for a day.
Dates must align to a whole UTC hour and the interval end must be at least 75
minutes old. Existing data and report files are preserved.

The downloader saves compressed typed BRTI observations (including intra-second
values and revision flags), frozen contract rules/references/outcomes and one-minute
quote closes. The audit verifies file hashes, requires all 60 exact-second samples
for each mean, and reports opening-reference and closing-outcome mismatches under
four explicit boundary/rounding hypotheses. It **does not choose a rule** or certify
that backfilled data was available at historical decision times. Read the report's
mismatch counts even when the command exits successfully. See the
[historical-data workflow](docs/HISTORICAL_DATA.md) for files, limits, exit codes and
remaining rule questions.

If a download stops during the index phase after contracts and quotes were saved,
version 0.6.1 can continue it without repeating completed downloads:

```bash
python -m kalshi_lab history --start 2026-10-25T00:00:00Z --hours 24 --run-dir data/history/btc-day-001 --resume --timeout 30
python -m kalshi_lab audit --run-dir data/history/btc-day-001 --out data/history/btc-day-001/settlement_audit_completed.json
```

Use the original interval and warm-up. Saved artifacts are verified and preserved;
only missing index hours are fetched. The prior manifest and audit report remain
available. History requests now retry transient failures at most twice; use
`--retries 0` to disable retries. See the historical-data guide for recovery limits.

## What happens after the settlement audit?

1. Acquire compact historical index returns, frozen contract metadata, quotes and
   settled outcomes; audit their timestamps and availability.
2. Add rolling/EWMA volatility estimates using only earlier observations.
3. Generate probabilities at 10, 5 and 2 minutes before close, initially excluding
   decision times inside the final averaging minute.
4. Compare probability scores and calibration on chronological held-out contracts.
5. Assess executable prices, fees, delays and size separately if forecasts warrant it.

The forecast and diagnostic commands below implement the retrospective experiment.
No held-out performance or net-profit claim is established by those commands.

## Repository scope

This repository exposes `digital`, `diagnose`, `history`, `audit`, `forecast`,
`analyze`, `compare`, `batch`, `shadow`, `paper` and `fees`.
REST signing and metadata helpers support read-only data access. The old
order-flow collector, book reconstruction, quote-change datasets, demos and their
guides have been removed from the active code; Git history retains them. Existing
local pilot files under `data/` are not deleted or migrated by this cleanup. Use
the earlier revision in a separate checkout if you need the retired commands.

Install `.[data]` for authenticated REST and WebSocket data access. The former `stream` extra
has been removed. The package/import names are stable, so an existing checkout
and virtual environment can be updated with the installation command above.

The fee calculator remains available:

```bash
python -m kalshi_lab fees --input examples/fee_orders.json --balance-precision 0.0001
```

GitHub Actions runs offline tests and examples on Python 3.10, 3.12 and 3.14.
Tests use generated keys and mocked transport; no live credentials are required.
Primary references and access dates are in [SOURCES.md](docs/SOURCES.md).

## Offline probability forecasts

The rolling/EWMA experiment is now available after a complete settlement audit:

```bash
python -m kalshi_lab forecast --run-dir data/history/btc-day-001 --out-dir data/forecasts/btc-day-001
```

It produces prediction records, matched evaluation rows and scores at 10, 5 and
2 minutes before close. See [FORECASTING.md](docs/FORECASTING.md) for assumptions,
Monte Carlo precision, exclusions and retrospective availability limitations.

Inspect an existing run without rerunning simulations or changing the model:

```bash
python -m kalshi_lab analyze --run-dir data/history/btc-day-001 --forecast-dir data/forecasts/btc-day-001 --out-dir data/analysis/btc-day-001
```

Open `data/analysis/btc-day-001/summary.md` for paired losses, the largest errors
and calibration by horizon. Detailed rows and past-return/standardized-gap panels
are saved alongside it. See [FORECAST_DIAGNOSTICS.md](docs/FORECAST_DIAGNOSTICS.md)
for field definitions and interpretation.

## Multi-day evaluation and live observation

Version 0.9 can reuse completed daily runs and evaluate a larger fixed-model sample:

```bash
python -m kalshi_lab batch --start 2026-10-25T00:00:00Z --days 4 --root-dir data --out-dir data/batches/first-four-days
```

It recognizes existing `btc-day-001`/`btc-day-002` histories by their intervals,
verifies and reuses matching forecasts/analyses, and downloads only missing days.
The output comparison keeps daily and pooled scores, calibration, feature panels,
and paired day-block uncertainty when enough days exist.

To record a bounded live sample with local receipt times and hypothetical forecasts:

```bash
python -m kalshi_lab shadow --run-dir data/shadow/btc-live-001 --minutes 120
```

This needs the same local data-access credentials as `history` and the `[data]`
extra. It records BRTI, rules/references, bid/ask depth, availability gaps and
forecast completion times. No orders are placed. Offline `paper` evaluation uses
an explicitly supplied fee/latency/depth scenario after settlement labels arrive.
See [MULTIDAY_AND_SHADOW.md](docs/MULTIDAY_AND_SHADOW.md) for all four commands,
warm-up requirements, reuse behavior, assumptions, output files and exit codes.
