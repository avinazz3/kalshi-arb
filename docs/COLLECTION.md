# Collect successive contract windows

The `collect` command discovers and records successive Kalshi crypto windows.
It is a read-only coordinator around the existing discovery and fixed-window
recorder. Version 0.4 supports BTC/ETH, with optional SOL, in synchronized
900-second windows. No model, account/funding call or order execution is added.

## Start a bounded collection run

Install the streaming dependencies and configure the same local Kalshi credentials
as the [joint pilot](../README.md#joint-kalshi--coinbase-pilot), then run:

```bash
python3 -m kalshi_lab collect --assets BTC ETH --seconds 3600 --run-dir data/runs/pilot-001
```

Choose a new directory. Existing runs are never overwritten or resumed. The default
runtime budget is one hour; the maximum is 86400 seconds. These are scheduling
budgets, not promises of that much usable data. Discovery, setup, rollover gaps and
invalid book intervals reduce coverage. A run starting partway through a window
records its remaining portion. The last capture can end early at the run budget.

`capture` still records a single manifest/window; `collect` handles discovery and
rollover for you. There is no need to run `discover` first for `collect`.
Credentials are preflighted before creating the run directory or calling HTTP.
Keep the key file outside the repository; authentication headers, key contents,
key IDs and credential paths are not stored in collection artifacts.

## How rollover works

1. Archive public series, event and market responses for every requested asset.
2. Validate that the active contracts have matching open/close times and a
   900-second duration. Use metadata, never a constructed next ticker or assumed
   wall-clock quarter-hour schedule.
3. Start a new capture with its own immutable manifest, database and session.
   It ends at the runtime budget or two seconds before contract close.
4. Flush and inspect the capture; checkpoint its quality report and coverage.
5. Wait until after that contract's recorded close, then rediscover all assets.

Both sockets reconnect for each window and obtain fresh snapshots. The coordinator
does not carry books, trade history, features or sequence numbers into another
contract. Each market ticker gets at most one capture attempt in a run, even if
that attempt lacked required snapshots. A subsequent run can record it again.

Default minimum remaining capture time is 30 seconds, excluding the two-second
close guard. Near-expiry windows are archived and skipped. If assets temporarily
refer to different windows or no active market is available, discovery is retried.
Unexpected schema, missing requested assets or a non-900-second synchronized
window stops the run rather than silently changing the experiment.

Options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--seconds` | 3600 | Monotonic scheduling budget; 1–86400, at least the minimum window time |
| `--assets` | BTC ETH | Explicit assets; optional SOL |
| `--minimum-window-seconds` | 30 | Minimum capture time to begin a window; at most 3600 |
| `--retry-seconds` | 5 | Availability polling interval and initial metadata-error backoff |
| `--max-discovery-failures` | 5 | Consecutive retryable HTTP/transport failures before stopping |

Transient metadata failures (transport errors, HTTP 429 and 5xx) use exponential
backoff, capped at 32 times the initial interval, and honor numeric `Retry-After`
delays if longer. A successful discovery or an explicit absence of active markets
resets the transport failure counter. Waiting for unavailable markets is bounded
by the run budget. Waits are interruptible and occur in chunks of at most 30s.

The coordinator gives each metadata HTTP operation at most 10s, shortened to the
remaining run budget, and checks the deadline before another operation. An
in-flight blocking HTTP operation and socket/writer cleanup can finish after the
scheduling deadline. Cancellation signals the metadata worker and waits for it
to close its archive before finalizing the report. This is not a hard real-time
deadline; socket timeouts do not guarantee a total response-download bound.

## Artifacts and failure handling

The run directory contains `run.json` and numbered `attempt-00001`, etc. Each
attempt can contain:

| File | Contents |
| --- | --- |
| `metadata.sqlite` | Raw HTTP attempts, including failures, hashes and metadata session IDs |
| `universe.json` | Full selected market/event/series metadata if discovery completed |
| `capture.sqlite` | Raw two-venue capture, fixed manifest and independent capture session |
| `quality.json` | Frame counts, hashes, sequences, snapshots and observation bounds |

Failed/partial discovery attempts may contain only the HTTP archive. Never count
attempt directories as captured windows. `run.json` is checkpointed with atomic
replacement as work progresses, so it can be read while the collector runs.
It records attempt statuses, contract mappings, capture session IDs, planned stop
reasons and per-venue coverage. Paths within it are relative to the run directory.

`captured` means the recorder completed and found a snapshot for each required
product/market. It does **not** mean a complete 15-minute window or a valid
research sample. `incomplete` means required snapshots were missing: retain its
raw data and quality report, then advance to the next window. Authentication,
non-retryable metadata errors, unexpected capture exceptions and storage failures
stop the run. Error types and HTTP status codes are reported without persisting
exception messages, which may contain sensitive details. Earlier windows remain.

A run with no capture that passes required-snapshot checks exits unsuccessfully,
even if its runtime budget expired normally. Its report says `no_complete_windows`.
Ctrl-C marks the run `interrupted` and retains available partial data/quality.
A hard process kill can leave `running` in the last checkpoint and lose the final
uncommitted SQLite batch; it cannot execute graceful finalization. Disk failure
may prevent the final report from being written.

Coverage uses actual first/last frame receipt times, not the requested duration.
Gaps between windows are differences between one window's last frame and the next
window's first frame for that venue, on this process's common monotonic clock.
Those spans do not measure internal gaps or uninterrupted valid book coverage;
inspect quality and dataset rejection counts too. For example, a synthetic capture
requested for 898s but emitting only 12s must report a 12s observation span.

This implementation deliberately has gaps: it stops before expiry and rediscovers,
reconnects and warms up the next window. Coinbase is also reconnected. Do not
describe these recordings as seamless or use cross-window labels. Frame-based
liveness still has the quiet-connection limitation described in [DATASET.md](DATASET.md).

## Inspect and build datasets

After a run ends, review `run.json`. For each captured attempt:

```bash
python3 -m kalshi_lab replay --db data/runs/pilot-001/attempt-00001/capture.sqlite
python3 -m kalshi_lab dataset --db data/runs/pilot-001/attempt-00001/capture.sqlite --out data/runs/pilot-001/attempt-00001/research.jsonl
```

The attempt directory may differ if early discoveries were skipped. Each capture
database has one session, so `--session` is unnecessary. For a first batch export:

```python
import json
from pathlib import Path
from kalshi_lab.dataset import export_dataset

root = Path("data/runs/pilot-001")
report = json.loads((root / "run.json").read_text())
for attempt in report["attempts"]:
    if attempt["status"] != "captured":
        continue
    folder = root / attempt["directory"]
    result = export_dataset(folder / "capture.sqlite", folder / "research.jsonl")
    print(attempt["session"], result["accepted_rows"], result["rejected_rows"])
```

Exports refuse existing output paths. Review zero-row exports and rejected rows;
do not turn missing observations into zero returns or merge book states across
windows. Keep full contract windows together in future chronological splits.

## Verify without credentials

```bash
python3 -m unittest discover -s tests -p test_collection.py -v
```

The two-window integration test uses a simulated clock and explicitly synthetic
feeds. It creates separate BTC/ETH manifests and capture databases, replays both
and generates datasets independently. Other tests inject metadata outages,
rate limits, authentication/storage errors, missing snapshots, stale/mismatched
windows and cancellation. No credentials or live services are used by these tests.
They validate scheduling and isolation; live authenticated rollover still requires
a supervised local pilot and inspection of its actual output.
