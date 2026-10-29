"""Sequential daily history/audit/forecast/analysis with verified reuse."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .analysis import load_rows, run_analysis
from .compare import json_file, load_analysis, run_compare
from .forecast import run_forecast
from .history import digest, fetch_history, save_json, window
from .markets import parse_time
from .settlement import audit_dataset, audit_loaded, load_dataset


def existing_history(root, day):
    canonical = root / 'history' / ('btc-' + day.strftime('%Y%m%d'))
    if canonical.exists() or not (root / 'history').exists():
        return canonical
    children = list((root / 'history').iterdir())
    if len(children) > 200:
        raise ValueError('Too many historical directories; use a smaller batch root')
    matches = []
    for child in children:
        path = child / 'manifest.json'
        if not child.is_dir() or not path.is_file():
            continue
        try:
            manifest = json_file(path)
            settings = manifest['settings']
            if (manifest.get('asset') == 'BTC' and manifest.get('index_id') == 'BRTI'
                    and parse_time(settings['start']) == day and settings['hours'] == 24 and settings['warmup_hours'] == 1):
                matches.append(child)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if len(matches) > 1:
        raise ValueError('Multiple existing history runs match this day; use a root with one selected run')
    return matches[0] if matches else canonical


def run_batch(start, days, root_dir, out_dir, *, paths=2000, progress=None, downloader=None, now=None):
    if type(days) is not int or not 1 <= days <= 30 or type(paths) is not int or not 100 <= paths <= 20000:
        raise ValueError('days must be 1..30 and paths 100..20000')
    now = now or datetime.now(timezone.utc)
    first = parse_time(start).astimezone(timezone.utc)
    if first.hour or first.minute or first.second or first.microsecond:
        raise ValueError('Batch start must be UTC midnight')
    window((first + timedelta(days=days - 1)).isoformat(), 24, 1, now)
    root, output = Path(root_dir), Path(out_dir)
    if output.exists():
        raise ValueError('Batch output already exists; choose a new directory')
    downloader = downloader or fetch_history
    output.mkdir(parents=True, exist_ok=False)
    report = {'schema_version': 1, 'package_version': __version__, 'status': 'running',
              'start': first.isoformat(), 'days': days, 'paths': paths, 'runs': [],
              'interpretation': 'Sequential fixed-model evaluation; no model selection or orders.'}
    save_json(output / 'batch.json', report)
    analyses = []
    try:
        for offset in range(days):
            day = first + timedelta(days=offset)
            name = 'btc-' + day.strftime('%Y%m%d')
            history = existing_history(root, day)
            forecast, analysis = (root / kind / history.name for kind in ('forecasts', 'analysis'))
            item = {'utc_day': day.date().isoformat(), 'history': str(history), 'forecast': str(forecast), 'analysis': str(analysis)}
            report['runs'].append(item)
            if history.exists() and json_file(history / 'manifest.json')['status'] == 'downloaded':
                data = load_dataset(history)
                if parse_time(data['settings']['start']) != day or data['settings']['hours'] != 24 or data['settings']['warmup_hours'] != 1:
                    raise ValueError('Existing history interval differs from batch day')
                audit = audit_loaded(data)
                item['history_reused'] = True
            else:
                result = downloader(history, start=day.isoformat(), hours=24, warmup_hours=1, resume=history.exists())
                if result['status'] != 'downloaded':
                    raise ValueError('History did not complete for ' + name + '; rerun into a new batch output to resume')
                audit = audit_dataset(history)
                item['history_reused'] = False
            if not (history / 'settlement_audit.json').exists():
                save_json(history / 'settlement_audit.json', audit)
            if forecast.exists():
                saved = json_file(forecast / 'report.json')
                config = saved['config']
                if any(config[k] != v for k, v in {'paths': paths, 'seed': 1729, 'lookback_minutes': 60, 'ewma_half_life_minutes': 15}.items()):
                    raise ValueError('Existing forecast settings differ; use a separate root directory')
                load_rows(history, forecast)
                item['forecast_reused'] = True
            else:
                run_forecast(history, forecast, paths=paths)
                item['forecast_reused'] = False
            if analysis.exists():
                _, saved = load_analysis(analysis)
                if saved['forecast_report_sha256'] != digest(forecast / 'report.json'):
                    raise ValueError('Existing analysis belongs to a different forecast')
                item['analysis_reused'] = True
            else:
                run_analysis(history, forecast, analysis)
                item['analysis_reused'] = False
            analyses.append(analysis)
            save_json(output / 'batch.json', report)
            if progress:
                progress(offset + 1, days)
        comparison = run_compare(analyses, output / 'comparison')
        report.update(status='completed', rows=comparison['rows'], contracts=comparison['contracts'],
                      comparison=str(output / 'comparison' / 'report.json'))
    except KeyboardInterrupt:
        report['status'] = 'interrupted'
        raise
    except Exception:
        report['status'] = 'failed'
        raise
    finally:
        save_json(output / 'batch.json', report)
    return report
