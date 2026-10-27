# Kalshi Bitcoin Probability Lab

**Research question:** can a settlement-aware model of Bitcoin prices and
volatility improve forecasts of BTC 15-minute contract outcomes relative to
Kalshi's quoted probabilities?

The current milestone provides an offline digital-option benchmark and a small,
read-only data-access diagnostic. The model takes explicit inputs; it does not
estimate volatility, fit a forecasting model, place orders or report trading
performance. The example is synthetic. See the
[research specification](docs/RESEARCH_SPEC.md) and
[model mathematics and verification](docs/DIGITAL_MODEL.md).

## Install and verify

Use Python 3.10 or newer. From your existing checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[stream]'
python tests/run_checks.py
```

If your virtual environment already exists, activate it and reinstall instead of
creating it again. The digital benchmark uses the standard library. The optional
`stream` dependencies support authenticated data checks, the retained recorder,
and the full test suite. Keep the distribution name `kalshi-crypto-flow` for
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

## What happens after access is confirmed?

1. Acquire compact historical index returns, frozen contract metadata, quotes and
   settled outcomes; audit their timestamps and availability.
2. Add rolling/EWMA volatility estimates using only earlier observations.
3. Generate probabilities at 10, 5 and 2 minutes before close, initially excluding
   decision times inside the final averaging minute.
4. Compare probability scores and calibration on chronological held-out contracts.
5. Assess executable prices, fees, delays and size separately if forecasts warrant it.

No historical forecasting scores or net-profit claims exist in this milestone.

## Existing work and reusable components

Continue using this repository. Discovery, metadata, authentication and fee
arithmetic remain useful. Detailed order-flow capture, book reconstruction and
quote-change datasets are retained as a separate reproducible experiment; they
are not required to run the new offline model. The original instructions are in
[the legacy order-flow guide](docs/ORDER_FLOW.md), with the historical
[order-flow specification](docs/ORDER_FLOW_SPEC.md). Existing pilot files remain
unchanged, and their old dataset command still targets 1s/5s quote movements.

The fee calculator remains available:

```bash
python -m kalshi_lab fees --input examples/fee_orders.json --balance-precision 0.0001
```

GitHub Actions runs offline tests and examples on Python 3.10, 3.12 and 3.14.
Tests use generated keys and mocked transport; no live credentials are required.
Primary references and access dates are in [SOURCES.md](docs/SOURCES.md).
