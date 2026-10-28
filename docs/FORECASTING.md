# Retrospective probability experiment

Run after completing the historical download:

```bash
python -m kalshi_lab forecast --run-dir data/history/btc-day-001 --out-dir data/forecasts/btc-day-001
```

This is offline and needs no credentials. It verifies dataset hashes and requires
complete coverage plus matching left-closed/half-up settlement reconstruction.
Existing output directories are refused. Default computation uses 2,000 independent
paths per model per decision and may take several minutes. Progress prints every
four contracts. `--paths 5000` increases precision; total work is capped at 250 million
path transitions. Other options are `--lookback 60`, `--half-life 15`, `--seed 1729`.
These are declared engineering defaults, not tuned or proven optimal parameters.

At nominal decision t (10, 5 or 2 minutes before close T), the spot timestamp is
t−1 second. Minute returns use exactly lookback+1 prices spaced 60 seconds apart,
ending at that spot timestamp. Missing, amended or repeat-flagged input endpoints
exclude the forecast. No interpolation occurs. Rolling variance is mean squared
log return, assuming zero mean for volatility estimation. Finite-window EWMA uses
weights proportional to 2^(-age/half-life), normalized across the same window.
Annual variance multiplies minute variance by 31,536,000/60. This is a finite-window
adaptation of exponential weighting, not a fitted GARCH model. Ignoring minute
return means in variance estimation is an approximation; GBM propagation still
uses zero price drift and log drift −sigma²/2.

Volatility remains fixed over each forecast. GBM is simulated at the sixty actual
settlement timestamps T−60,...,T−1; the arithmetic mean is rounded half-up to cents
and compared with the published reference. Because spot is timestamped t−1, a
10-minute forecast has 601 seconds from source to close and sample offsets 541–600.
The separate synthetic digital example starts at t and therefore uses 540–599.
Shared random seeds across the two volatility models reduce comparison noise;
paths within each model are independent. Wilson intervals and standard errors
measure Monte Carlo uncertainty only. At 2,000 paths the largest binomial standard
error is approximately 0.0112. Zero successful paths does not prove zero probability.

`predictions.jsonl` records inputs, timestamps, seed, volatility, probabilities and
simulation uncertainty without outcomes. `evaluation.csv` attaches actual outcomes
and the bid/ask midpoint of the candle ending exactly at the decision timestamp.
Missing/invalid matching quotes exclude that decision from all three scoring panels.
`report.json` records provenance, configuration, exclusions, and common-row Brier
and log-loss scores overall and separately by horizon. Brier uses raw probabilities;
log loss clips every model and benchmark to [1e-6,1−1e-6], with clip counts reported.
Lower scores are better. Exit 0 means all planned decisions were scored; exit 2
means exclusions occurred. Interrupted/failed runs retain a status report; use a
new output directory to rerun. There is no automatic forecast resume.

Minute candles are retrospective aggregates, not executable quotes with size or
original receipt times. Backfilled index prices and opening references are not
certified available at past decision times. Settlement matching is a retrospective
data validation gate, not an input to probability estimation. No hyperparameters
are selected from outcomes. The three forecasts per contract share one outcome;
288 rows are not 288 independent contracts. No significance or profit claim is made.
Use this day to verify machinery, then freeze settings and evaluate untouched later
days. No execution, fees-based trading evaluation or automatic tuning is included.

References: RiskMetrics Technical Document (1996), section 5.2, for exponential
weighting (https://www.msci.com/documents/10199/5915b101-4206-4ba0-aee2-3449d5c7e95a);
Gneiting and Raftery (2007) for proper probability scores
(https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jasa.pdf).
