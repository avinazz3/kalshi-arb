"""Compare frozen analysis runs; paired moving-block bootstrap of UTC days."""
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random

from . import __version__
from .analysis import _csv, calibration, panel_summary
from .forecast import HORIZONS, MODELS, losses, summarize
from .history import digest, save_json


def json_file(path):
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Comparison input exceeds size budget')
    return json.loads(path.read_text(encoding='utf-8'))


def load_analysis(directory):
    directory = Path(directory)
    report = json_file(directory / 'report.json')
    if report.get('schema_version') != 1 or report.get('status') != 'completed' or 'forecast_report_sha256' not in report:
        raise ValueError('Expected a completed single-run analysis report')
    config_hash = hashlib.sha256(json.dumps(report['config'], sort_keys=True).encode()).hexdigest()
    if config_hash != report['config_sha256']:
        raise ValueError('Analysis configuration hash mismatch')
    for name in ('rows.csv', 'calibration.csv', 'summary.md'):
        path = directory / name
        if path.stat().st_size > 16 * 1024 * 1024 or digest(path) != report['artifacts'][name]:
            raise ValueError('Analysis artifact hash mismatch: ' + name)
    rows, seen, contracts = [], set(), {}
    with (directory / 'rows.csv').open(newline='', encoding='utf-8') as handle:
        for raw in csv.DictReader(handle):
            if len(rows) >= 288:
                raise ValueError('Single-run row budget exceeded')
            row = dict(raw)
            for key in ('horizon_minutes', 'decision_ts', 'source_ts', 'outcome'):
                row[key] = int(raw[key])
            if row['horizon_minutes'] not in HORIZONS or row['outcome'] not in (0, 1):
                raise ValueError('Invalid analysis identity or outcome')
            close = row['decision_ts'] + row['horizon_minutes'] * 60
            if close % 900 or row['source_ts'] != row['decision_ts'] - 1:
                raise ValueError('Invalid analysis timestamps')
            key = row['ticker'], row['horizon_minutes']
            if key in seen or contracts.get(row['ticker'], (close, row['outcome'])) != (close, row['outcome']):
                raise ValueError('Duplicate forecast or inconsistent contract')
            seen.add(key)
            contracts[row['ticker']] = close, row['outcome']
            for model in MODELS:
                row[model] = float(raw[model])
                for metric, value in losses(row[model], row['outcome']).items():
                    saved = raw[f'{model}_{metric}']
                    if metric == 'log_clipped':
                        if saved != str(value):
                            raise ValueError('Inconsistent clipping flag')
                    elif not math.isclose(float(saved), value, abs_tol=1e-12, rel_tol=1e-12):
                        raise ValueError('Inconsistent saved score')
                    row[f'{model}_{metric}'] = value
            for model in ('rolling', 'ewma'):
                for metric in ('brier', 'log_loss'):
                    value = row[f'{model}_{metric}'] - row[f'market_{metric}']
                    if not math.isclose(float(raw[f'{model}_excess_{metric}']), value, abs_tol=1e-12, rel_tol=1e-12):
                        raise ValueError('Inconsistent paired loss')
                    row[f'{model}_excess_{metric}'] = value
                value = raw[f'{model}_gap_z']
                row[f'{model}_gap_z'] = float(value) if value else None
                if value and not math.isfinite(row[f'{model}_gap_z']):
                    raise ValueError('Invalid standardized gap')
            for minutes in (1, 2, 5):
                key = f'return_{minutes}m_log'
                row[key] = float(raw[key]) if raw[key] else None
                if row[key] is not None and not math.isfinite(row[key]):
                    raise ValueError('Invalid past return')
            row['open_ts'] = close - 900
            row['utc_day'] = datetime.fromtimestamp(close - 900, timezone.utc).date().isoformat()
            rows.append(row)
    if not rows or len(rows) != report['rows'] or len(contracts) != report['contracts']:
        raise ValueError('Analysis row counts do not reconcile')
    computed = summarize(rows)
    for panel, values in computed.items():
        if report['scores'][panel]['rows'] != values['rows']:
            raise ValueError('Analysis score counts do not reconcile')
        for model in MODELS:
            for metric in ('brier', 'log_loss'):
                saved, actual = report['scores'][panel]['models'][model][metric], values['models'][model][metric]
                if saved != actual and (saved is None or actual is None or not math.isclose(saved, actual, abs_tol=1e-12)):
                    raise ValueError('Analysis scores do not reconcile')
    return rows, report


def quantile(values, p):
    values = sorted(values)
    offset = (len(values) - 1) * p
    lower = int(offset)
    return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (offset - lower)


def day_bootstrap(rows, *, block_days=2, samples=2000, seed=314159):
    if type(block_days) is not int or not 1 <= block_days <= 7 or type(samples) is not int or not 100 <= samples <= 20000 or type(seed) is not int:
        raise ValueError('block-days must be 1..7; samples 100..20000; seed an integer')
    days = sorted({r['utc_day'] for r in rows})
    stamps = [int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp()) for d in days]
    blocks = [list(range(i, i + block_days)) for i in range(len(days) - block_days + 1)
              if stamps[i + block_days - 1] - stamps[i] == (block_days - 1) * 86400]
    result = {'method': 'paired_non_circular_moving_blocks_of_UTC_contract_open_days',
              'block_days': block_days, 'samples': samples, 'seed': seed, 'days': len(days),
              'available_blocks': len(blocks), 'minimum_days': max(8, 4 * block_days),
              'interpretation': 'Exploratory percentile intervals conditional on these dates and a chosen block length; not a guarantee of coverage.'}
    if len(days) < result['minimum_days'] or len(blocks) < 4:
        return {**result, 'status': 'unavailable', 'reason': 'too_few_days_or_contiguous_blocks', 'intervals': {}}
    panels = [('overall', rows)] + [(f'{h}min', [r for r in rows if r['horizon_minutes'] == h]) for h in HORIZONS]
    totals = {}
    for name, group in panels:
        for model in ('rolling', 'ewma'):
            for metric in ('brier', 'log_loss'):
                totals[name, model, metric] = [(math.fsum(r[f'{model}_excess_{metric}'] for r in group if r['utc_day'] == day),
                                               sum(r['utc_day'] == day for r in group)) for day in days]
    draws = {key: [] for key in totals}
    rng = random.Random(seed)
    for _ in range(samples):
        selected = []
        while len(selected) < len(days):
            selected.extend(rng.choice(blocks))
        selected = selected[:len(days)]
        for key, daily in totals.items():
            count = sum(daily[i][1] for i in selected)
            if count:
                draws[key].append(math.fsum(daily[i][0] for i in selected) / count)
    intervals = {}
    for (panel, model, metric), values in draws.items():
        intervals.setdefault(panel, {}).setdefault(model, {})[metric] = {
            'percentile_95': [quantile(values, .025), quantile(values, .975)] if values else None,
            'valid_replicates': len(values)}
    return {**result, 'status': 'computed', 'intervals': intervals}


def run_compare(analysis_dirs, out_dir, *, block_days=2, samples=2000, seed=314159):
    output = Path(out_dir)
    if output.exists():
        raise ValueError('Comparison output already exists; choose a new directory')
    if not 1 <= len(analysis_dirs) <= 90:
        raise ValueError('Supply 1..90 analysis directories')
    rows, sources, seen, intervals, config = [], [], set(), [], None
    for n, directory in enumerate(analysis_dirs):
        group, report = load_analysis(directory)
        if config is not None and report['config'] != config:
            raise ValueError('Cannot pool different forecast settings')
        config = report['config']
        identities = {r['ticker'] for r in group}
        lower, upper = min(r['open_ts'] for r in group), max(r['open_ts'] for r in group) + 900
        if seen & identities or any(lower < b and a < upper for a, b in intervals):
            raise ValueError('Duplicate or overlapping contract intervals')
        seen.update(identities)
        intervals.append((lower, upper))
        for row in group:
            row['source_run'] = n
        rows.extend(group)
        sources.append({'source_run': n, 'directory': str(directory), 'report_sha256': digest(Path(directory) / 'report.json'),
                        'history_manifest_sha256': report['history_manifest_sha256'],
                        'config_sha256': report['config_sha256'], 'rows': len(group),
                        'source_status': report['source_status'], 'source_exclusion_counts': report['source_exclusion_counts']})
    rows.sort(key=lambda r: (r['decision_ts'], r['ticker']))
    bootstrap = day_bootstrap(rows, block_days=block_days, samples=samples, seed=seed)
    daily, daily_csv = [], []
    for day in sorted({r['utc_day'] for r in rows}):
        group = [r for r in rows if r['utc_day'] == day]
        scores = summarize(group)
        daily.append({'utc_day': day, 'contracts': len({r['ticker'] for r in group}), 'scores': scores})
        for panel, values in scores.items():
            for model in MODELS:
                s, benchmark = values['models'][model], values['models']['market']
                daily_csv.append({'utc_day': day, 'horizon': panel, 'model': model, 'rows': values['rows'],
                                  'brier': s['brier'], 'log_loss': s['log_loss'],
                                  'excess_brier': s['brier'] - benchmark['brier'] if values['rows'] else None,
                                  'excess_log_loss': s['log_loss'] - benchmark['log_loss'] if values['rows'] else None})
    report = {'schema_version': 1, 'package_version': __version__, 'status': 'completed', 'sources': sources,
              'config': config, 'rows': len(rows), 'contracts': len(seen), 'utc_days': len(daily),
              'scores': summarize(rows), 'daily': daily, 'panels': panel_summary(rows), 'bootstrap': bootstrap,
              'point_in_time_certified': False, 'interpretation': 'Fixed-model retrospective comparisons; development and test design must be declared separately.'}
    lines = ['# Multi-day forecast comparison', '', f"{len(daily)} UTC days; {len(seen)} contracts; {len(rows)} matched forecasts.", '',
             'Lower scores are better. Positive excess loss means worse than the market.', '',
             '| Horizon | Rolling Brier | EWMA Brier | Market Brier |', '|---|---:|---:|---:|']
    for panel, values in report['scores'].items():
        f = lambda m: '—' if values['models'][m]['brier'] is None else f"{values['models'][m]['brier']:.6f}"
        lines.append(f"| {panel} | {f('rolling')} | {f('ewma')} | {f('market')} |")
    lines += ['', '| UTC day | Contracts | Rolling overall Brier | Market overall Brier |', '|---|---:|---:|---:|']
    for day in daily:
        scores = day['scores']['overall']['models']
        lines.append(f"| {day['utc_day']} | {day['contracts']} | {scores['rolling']['brier']:.6f} | {scores['market']['brier']:.6f} |")
    lines += ['', f"Day-block bootstrap: **{bootstrap['status']}**; block length {block_days} days.", '',
              'Intervals are omitted below the declared minimum number of days/blocks. When computed, report.json contains paired 95% percentile intervals. '
              'Adjacent days may still have dependence beyond the chosen block length; inspect sensitivity before drawing conclusions.', '',
              'Pooled calibration and feature panels use common scored rows. Partial source exclusions remain visible. '
              'Forecasts at different horizons share outcomes. Candle midpoints do not establish executable returns.', '']
    output.mkdir(parents=True, exist_ok=False)
    _csv(output / 'daily_scores.csv', daily_csv)
    _csv(output / 'rows.csv', rows)
    _csv(output / 'calibration.csv', calibration(rows))
    (output / 'summary.md').write_text('\n'.join(lines), encoding='utf-8')
    report['artifacts'] = {name: digest(output / name) for name in ('daily_scores.csv', 'rows.csv', 'calibration.csv', 'summary.md')}
    save_json(output / 'report.json', report)
    return report
