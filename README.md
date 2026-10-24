# Kalshi Arbitrage Lab — starter v0.1

**Start here.** This is the first working milestone of *From Price Inconsistency
to Executable Arbitrage*. It collects public market data and demonstrates exact
payoff accounting. It does not yet establish a trading edge.

## Your first 15 minutes

Clone this repository and check Python (or extract the downloaded starter and
open a terminal inside its folder):

```bash
git clone https://github.com/avinazz3/kalshi-arb.git
cd kalshi-arb
python3 --version
```

Use Python 3.10 or newer. No third-party packages, API keys or funding are needed
for this milestone. On Windows use `py` in place of `python3` if appropriate.

While the initial implementation pull request is open, switch to its branch:

```bash
git switch codex/kalshi-research-starter
```

Run the offline example, then its checks:

```bash
python3 -m kalshi_lab demo
python3 -m unittest discover -s tests -v
```

The synthetic example buys 8 YES contracts on a lower threshold and 8 NO
contracts on a higher threshold. Assuming ordinary binary settlement and a valid
implication, minimum payout is $8, quoted acquisition cost is $7.68, and gross
surplus is $0.32. An **invented $0.20 total fee reserve** leaves $0.12. This reserve
is not an estimate of the current Kalshi fee. Expanding to 20 pairs reduces gross
surplus to $0.20 because the later pairs cost $1.01 each. This is illustrative,
not a recorded market opportunity.

## Discover the first research universe

```bash
python3 -m kalshi_lab discover --series KXCPI
```

This retrieves open CPI markets, handles pagination, saves raw HTTP responses,
and writes:

- `data/discovery/markets.json`: markets, series rule links and fee metadata.
- `data/discovery/selected_event.json`: event metadata, including fee overrides
  where returned by the API.
- `data/discovery/relation_candidates.json`: adjacent threshold proposals, all
  explicitly unreviewed.
- `data/discovery/watchlist.json`: four markets from one event for collection.
- `data/discovery/requests.sqlite`: exact raw response bodies, hashes and times.

The watchlist selects the highest-volume event **within the retrieved pages**,
then thresholds whose quoted midpoints are closest to $0.50. It is a collection
heuristic, not evidence of an edge or a relationship approval. Check
`pagination_complete`; an incomplete retrieval is not the full market universe.
If no open threshold family exists, discovery exits with an explanation. Do not
reuse expired tickers. An explicit `--event EVENT_TICKER` selects a particular
event from the retrieved universe. Old dated JSON files are evidence only.

## Record a short sample

```bash
python3 -m kalshi_lab collect --sweeps 12 --interval 5
python3 -m kalshi_lab inspect --out data/quality_report.json
```

Keep the computer awake and connected. Twelve sweeps target roughly a minute,
but slow responses and retries extend this. Stop with Ctrl+C if needed; each
response is committed immediately. To record until you stop it:

```bash
python3 -m kalshi_lab collect --sweeps 0 --interval 5
```

The collector archives fresh series, event and market metadata at startup, then
polls the selected books sequentially. `data/orderbooks.sqlite` contains all runs
with distinct session IDs. It stores UTC request/receipt times, monotonic request
duration, status, headers, raw bytes and a SHA-256 hash. Retries are retained too.
There is no silent forward-filling of failed responses. A JSON/schema failure
remains available in the raw log and is counted as invalid in the quality report.

Receipt time is when the full response has been read locally. It is not the
exchange matching-engine timestamp. The report's request duration is not a
measurement of exchange-to-client latency. A sweep is not an atomic market
snapshot. Five-second polling can miss short opportunities, so these samples
cannot establish absence of arbitrage, fill probability or opportunity duration.
Later milestones require WebSocket reconstruction and receipt-time replay.

## What to understand before the next session

1. Why buying YES at the lower threshold and NO at the higher threshold has a
   minimum ordinary binary payout of $1 per pair.
2. Why the reverse pairing does not have that guarantee.
3. Why buying YES uses the opposite NO bid ladder.
4. Why a positive snapshot surplus does not establish executable arbitrage.
5. Why fees, exceptional settlement and partial fills can invalidate a naive
   profit calculation.

Read `docs/RESEARCH_SPEC.md` next. After the short capture works, collect a day
of data and bring back `quality_report.json` plus `watchlist.json`. The database
is useful for deeper analysis; there is no need to send credentials.

## Current boundary

Implemented: public GET-only discovery and capture, metadata-derived proposals,
capture-quality report, exact depth/payoff accounting, offline economic checks.

Next: manual contract review, verified fee model and effective fee changes,
candidate scanning, depth-aware optimisation, streaming/replay, forward testing,
and eventually a separate execution component. No order-placement functionality
exists in this starter. Live access and deployment come after account eligibility
and operational testing.

See `docs/SOURCES.md` for official API references and the research reading list.

## Development checks

GitHub Actions runs offline unit tests, the synthetic demo and inspection of the
recorded pilot on Python 3.10 and 3.12. It makes no calls to Kalshi. The optional
console command is available after `python3 -m pip install -e .`:

```bash
kalshi-lab demo
```
