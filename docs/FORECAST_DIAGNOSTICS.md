# Diagnose existing forecasts

Keep the rolling/EWMA model and settings fixed while investigating its errors.
The analysis command reads the existing forecast artifacts without fitting a model,
changing any probabilities or rerunning Monte Carlo:

```bash
python -m kalshi_lab analyze --run-dir data/history/btc-day-001 --forecast-dir data/forecasts/btc-day-001 --out-dir data/analysis/btc-day-001
```

Use a new output directory for every analysis. Existing v0.7 forecast runs are
supported. No credentials or network calls are needed. The historical manifest,
frozen artifact hashes, forecast artifact hashes, configuration hash, settlement
reconstruction and exact forecast/evaluation joins are checked before output is
created. Spot prices, input timestamps, references and volatility estimates are
also checked against the historical inputs. These checks detect inconsistent
files; they are not cryptographic proof of historical availability or authenticity.

## Outputs

| File | Contents |
|---|---|
| `summary.md` | Score comparison, concentration of losses and calibration tables |
| `rows.csv` | One row per matched forecast, with probabilities, features, losses and labels |
| `calibration.csv` | Ten fixed probability bins for each model and forecast horizon |
| `report.json` | Provenance, scores, loss concentration, standardized-gap and past-return panels |

Only common rows with a valid matching market midpoint enter any comparison.
The source run's exclusions and number of unpaired predictions are reported.
A finished but incomplete forecast run is accepted; a diagnostic exit code of 0
means the report was produced, not that the source has complete coverage.
Invalid inputs exit 1. Inputs and existing outputs are preserved.

## Losses and concentration

Every row includes the Brier score, clipped log loss and clipping flag for rolling,
EWMA and the market. Log loss uses the forecast command's fixed clipping level
of 1e-6. Model excess loss is model loss minus market loss on that same row.
Positive means the model scored worse. Horizon panels report both the mean
and sum of excess Brier loss, win/tie/loss counts and mean excess log loss.

For each horizon/model, the three largest excess Brier losses are listed with
their sum and the sum across remaining rows. Those three rows are selected after
seeing outcomes and remain included in all full scores. This is a concentration
diagnostic; excluding them does not provide a valid alternative performance claim.
The report also counts exact 0/1 forecasts and how many were wrong. A correct
exact 0/1 forecast has zero Brier loss. Observed correctness on a sample does
not establish calibration.

## Past-only returns

At source timestamp `s = decision_ts - 1`, each feature is:

`return_Nm_log = log(S_s / S_(s-60*N))`, for N = 1, 2 and 5.

These are log returns, not percentage points. All endpoints precede the decision;
no nearest-tick lookup, interpolation or future observation is used. A missing,
amended or repeat-flagged historical endpoint produces an empty return and an
explicit status. It does not remove an otherwise valid scored forecast.
Horizon/model panels group losses by negative, zero, positive and unavailable
return. No direction is fed back into the forecasting model. The realized closing
average and outcome are attached as labels only.

## Standardizing the gap using the actual settlement schedule

Let C = 31,536,000 seconds/year, S be the source price, sigma the frozen annual
volatility, and a_i the sixty settlement offsets from the source in seconds.
For zero price-drift GBM, the covariance is exactly:

`Cov(S_(s+a_i), S_(s+a_j)) = S^2 * expm1(sigma^2 * min(a_i,a_j) / C)`.

The variance of the **unrounded arithmetic average** is the sum of these
covariances divided by 60^2. `average_sd_dollars` is its square root.
`terminal_sd_dollars` uses `S * sqrt(expm1(sigma^2 * (T-s) / C))`.
These two uncertainty measures refer to different payoffs.

`gap_dollars` is S minus the published reference. The model's `gap_z` uses
`(S - effective_cutoff) / average_sd_dollars`, where the cutoff accounts for the
rounded comparison: K-0.005 for greater-or-equal and K+0.005 for strict greater.
Zero variance produces an empty z and a separate group. The fixed z groups are
less than -2, [-2,-1), [-1,1], (1,2], and greater than 2.

The exact variance calculation does not make the arithmetic average normal.
Do not convert z to a normal-CDF probability or replace the saved Monte Carlo
forecast with it. It is a descriptive measure of confidence relative to distance.

## Calibration and interpretation

Bins are [0.0,0.1), ..., [0.9,1.0], with 1.0 included in the last bin. Each reports
count, mean forecast probability and observed YES rate; empty bins have null
means. Models occupy different bins but use the same full evaluation sample.
Calibration is separated by horizon, including for the market benchmark.

Small bins fluctuate substantially. The three horizons share a contract outcome,
and consecutive contracts may also be dependent. No independent-row standard
errors or significance tests are inferred from these descriptive tables.
Historical candles are aggregated midpoint benchmarks without executable depth
or exact quote-update times. Saved index timestamps do not prove live availability.

The already-inspected day is development data. For another untouched day, download
and audit it, run the forecast command with the same settings, then run this
analysis into a separate directory. Compare all contracts, not just memorable
misses. Do not tune drift, momentum or volatility settings from these diagnostics
and present the same sample as held-out validation.

The Archak–Ipeirotis paper's contract-price volatility model is complementary to
this experiment. This command does not apply its terminal-payoff formula to the
60-price average or initialize an independent event forecast from the market quote.
The next model extension should follow evidence across additional days.
