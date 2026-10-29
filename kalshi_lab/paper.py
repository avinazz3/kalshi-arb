"""Offline FOK snapshot scenarios with explicit fees, latency and known labels."""
from collections import Counter
from datetime import timedelta
from decimal import Decimal
import json
import hashlib
from pathlib import Path

from . import __version__
from .analysis import _csv
from .compare import json_file
from .fees import FeePolicy, Fill, calculate_order
from .forecast import losses
from .history import digest, save_json
from .markets import parse_time
from .numbers import decimal
from .settlement import audit_loaded, load_dataset
from .shadow import rule_hash
from .volatility import estimate


def walk_asks(book, side, quantity, policy, precision, limit=None):
    """Consume only recorded opposite-side bids; insufficient size means no fill."""
    remaining, fills = quantity, []
    for bid, size in book['no_bids' if side == 'yes' else 'yes_bids']:
        price, size = 1 - decimal(bid), decimal(size)
        if limit is not None and price > limit:
            break
        take = min(remaining, size)
        fills.append(Fill(price, take, 'taker', 'buy'))
        remaining -= take
        if remaining == 0:
            result = calculate_order(fills, policy, precision)
            return {'cost': -result['revenue_dollars'], 'fee': result['net_fee_dollars'],
                    'cash_cost': -result['cash_change_dollars'], 'worst_price': price,
                    'fills': [[str(f.price_dollars), str(f.quantity)] for f in fills]}
    return None


def _events(path):
    if path.stat().st_size > 256 * 1024 * 1024:
        raise ValueError('Shadow file exceeds budget')
    with path.open(encoding='utf-8') as handle:
        for line in handle:
            yield json.loads(line)


def run_paper(shadow_dir, run_dirs, policy_path, out_dir):
    output, directory = Path(out_dir), Path(shadow_dir)
    if output.exists():
        raise ValueError('Paper output already exists; choose a new directory')
    settings = json_file(Path(policy_path))
    required = {'policy', 'balance_precision', 'quantity', 'minimum_net_edge_per_contract',
                'latency_ms', 'max_wait_ms', 'max_slippage_dollars'}
    if set(settings) != required:
        raise ValueError('Paper policy must contain exactly the documented fields')
    policy = FeePolicy(**settings['policy'])
    quantity, minimum, slippage = (decimal(settings[k]) for k in ('quantity', 'minimum_net_edge_per_contract', 'max_slippage_dollars'))
    precision = decimal(settings['balance_precision'])
    if (not 0 < quantity <= 1000 or quantity != quantity.quantize(Decimal('.01')) or not 0 <= minimum <= 1
            or not 0 <= slippage <= 1 or precision not in (Decimal('.01'), Decimal('.0001'))):
        raise ValueError('Invalid paper quantity, edge, slippage or precision')
    if any(type(settings[k]) is not int or not 0 <= settings[k] <= 60000 for k in ('latency_ms', 'max_wait_ms')):
        raise ValueError('Latency and maximum wait must be integer milliseconds 0..60000')
    source = json_file(directory / 'report.json')
    if source.get('kind') != 'shadow_recording' or source.get('schema_version') != 1 or source['status'] == 'running':
        raise ValueError('Finish or interrupt the shadow recorder before evaluating')
    for name in ('observations.jsonl', 'decisions.jsonl'):
        if digest(directory / name) != source['artifacts'][name]:
            raise ValueError('Shadow artifact hash mismatch')
    if not 1 <= len(run_dirs) <= 30:
        raise ValueError('Supply 1..30 historical label datasets')
    markets, label_sources = {}, []
    for run_dir in run_dirs:
        data = load_dataset(run_dir)
        audit = audit_loaded(data)
        selected = next(c for c in audit['candidates'] if c['boundary'] == 'left_closed' and c['rounding'] == 'half_up')
        if audit['status'] != 'computed' or any(v['mismatches'] or (k != 'expiration_value_matches' and v['unavailable']) for k, v in selected['counts'].items()):
            raise ValueError('Historical labels failed settlement reconciliation')
        for market in data['contracts']:
            if market['ticker'] in markets:
                raise ValueError('Duplicate historical label contract')
            markets[market['ticker']] = market
        label_sources.append({'directory': str(run_dir), 'manifest_sha256': audit['manifest_sha256']})
    observations, books, previous = {}, {}, None
    for event in _events(directory / 'observations.jsonl'):
        if type(event['id']) is not int or event['id'] in observations:
            raise ValueError('Duplicate observation identity')
        received, sent = parse_time(event['received_at']), parse_time(event['request_started_at'])
        if sent > received or previous is not None and received < previous:
            raise ValueError('Invalid observation clock ordering')
        previous = received
        observations[event['id']] = event
        if event['kind'] == 'book':
            books.setdefault(event['value']['ticker'], []).append(event)
    decisions = list(_events(directory / 'decisions.jsonl'))
    if len(decisions) > 3 * 96 * 2:
        raise ValueError('Shadow decision budget exceeded')
    decisions.sort(key=lambda d: parse_time(d['available_at']))
    attempts, attempted, counts = [], set(), Counter()
    for decision in decisions:
        if decision['status'] != 'forecast':
            counts['unavailable_decisions'] += 1
            continue
        ticker, ready = decision['ticker'], parse_time(decision['available_at'])
        prediction = decision['prediction']
        calculation = parse_time(prediction['calculation_started_at'])
        if calculation > ready or prediction['ticker'] != ticker:
            raise ValueError('Invalid decision availability')
        close = parse_time(decision['market']['close_time'])
        source_ts = prediction['source_ts']
        horizon = decision['horizon_minutes']
        if (horizon not in (10, 5, 2) or prediction['horizon_minutes'] != horizon
                or prediction['decision_ts'] != int(calculation.timestamp()) or type(source_ts) is not int
                or not source_ts < prediction['decision_ts'] or not 0 < calculation.timestamp() - source_ts <= 3
                or prediction['nominal_decision_ts'] != int(close.timestamp()) - horizon * 60
                or not 0 <= calculation.timestamp() - prediction['nominal_decision_ts'] <= 10
                or decimal(prediction['reference']) != decimal(decision['market']['reference'])
                or prediction['comparison'] != decision['market']['comparison']):
            raise ValueError('Invalid live forecast timestamp/reference alignment')
        inputs = prediction['input_observations']
        expected_times = [(prediction['source_ts'] - 60 * i) * 1000 for i in range(60, -1, -1)]
        if ([p['time_ms'] for p in inputs] != expected_times
                or any(p['amend_time_ms'] is not None or p['repeat_of_previous_value'] for p in inputs)
                or hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest() != prediction['input_sha256']
                or decision['rule_sha256'] != rule_hash(decision['market'])):
            raise ValueError('Forecast input/rule integrity mismatch')
        book_event = observations[decision['book_observation_id']]
        metadata = observations[decision['metadata_observation_id']]
        if (book_event['kind'] != 'book' or book_event['value']['ticker'] != ticker
                or metadata['kind'] != 'market' or metadata['value'] != decision['market']
                or book_event['value'] != decision['book'] or parse_time(book_event['received_at']) > calculation
                or parse_time(metadata['received_at']) > calculation):
            raise ValueError('Decision book/metadata did not exist at calculation time')
        for point in prediction['input_observations']:
            event = observations[point['observation_id']]
            if parse_time(event['received_at']) > calculation or event['kind'] not in ('index', 'bootstrap_index'):
                raise ValueError('Forecast input arrived after calculation')
            if not any(all(p[k] == point[k] for k in ('time_ms', 'value', 'amend_time_ms', 'repeat_of_previous_value')) for p in event['value']):
                raise ValueError('Forecast input does not match recorded observation')
        vols = estimate([p['value'] for p in inputs])
        for model in ('rolling', 'ewma'):
            saved = prediction['models'][model]
            if (saved['paths'] != source['config']['paths'] or type(saved['yes_paths']) is not int
                    or not 0 <= saved['yes_paths'] <= saved['paths']
                    or saved['probability'] != saved['yes_paths'] / saved['paths']
                    or saved['annual_volatility'] != vols[model + '_annual_volatility']):
                raise ValueError('Saved live probability/volatility counts do not reconcile')
        historical = markets.get(ticker)
        if historical is not None and (decimal(historical['reference']) != decimal(decision['market']['reference'])
                                       or historical['comparison'] != decision['market']['comparison']
                                       or any(historical[k] != decision['market'][k] for k in ('open_time', 'close_time', 'rules_primary', 'rules_secondary'))):
            raise ValueError('Observed reference/rules differ from settled label contract')
        eligible_at = ready + timedelta(milliseconds=settings['latency_ms'])
        latest_at = eligible_at + timedelta(milliseconds=settings['max_wait_ms'])
        future_book = next((b for b in books.get(ticker, []) if parse_time(b['request_started_at']) >= eligible_at
                            and parse_time(b['received_at']) <= latest_at and parse_time(b['received_at']) < close), None)
        for model in ('rolling', 'ewma'):
            if (ticker, model) in attempted:
                continue
            probability = prediction['models'][model]['probability']
            losses(probability, 0)
            alternatives = []
            for side, p in (('yes', decimal(str(probability))), ('no', 1 - decimal(str(probability)))):
                quoted = walk_asks(decision['book'], side, quantity, policy, precision)
                if quoted and quantity * p - quoted['cash_cost'] >= quantity * minimum:
                    alternatives.append((quantity * p - quoted['cash_cost'], side, quoted, p))
            if not alternatives:
                counts[model + ':no_signal'] += 1
                continue
            _, side, quoted, p = max(alternatives, key=lambda x: x[0])
            attempted.add((ticker, model))  # One attempt, including failures, per contract/model.
            row = {'ticker': ticker, 'model': model, 'horizon_minutes': decision['horizon_minutes'], 'side': side,
                   'decision_available_at': ready.isoformat(), 'quantity': str(quantity),
                   'forecast_probability_for_side': str(p), 'quoted_cash_cost': str(quoted['cash_cost']),
                   'quoted_expected_net_profit': str(quantity * p - quoted['cash_cost']),
                   'status': 'no_post_latency_book', 'execution_observation_id': None, 'execution_received_at': None,
                   'execution_cost': None, 'execution_fee': None, 'execution_cash_cost': None,
                   'assumed_fills': None, 'payout': None, 'net_profit': None}
            if future_book:
                filled = walk_asks(future_book['value'], side, quantity, policy, precision,
                                   limit=min(Decimal(1), quoted['worst_price'] + slippage))
                row.update(execution_observation_id=future_book['id'], execution_received_at=future_book['received_at'])
                if filled is None:
                    row['status'] = 'unfilled_depth_or_price_limit'
                else:
                    row.update(status='assumed_filled_pending_label', execution_cost=str(filled['cost']), execution_fee=str(filled['fee']),
                               execution_cash_cost=str(filled['cash_cost']), assumed_fills=json.dumps(filled['fills']))
                    if historical:
                        payout = quantity if historical['result'] == side else Decimal(0)
                        row.update(status='assumed_filled_scored', payout=str(payout), net_profit=str(payout - filled['cash_cost']))
            counts[model + ':' + row['status']] += 1
            attempts.append(row)
    models = {}
    for model in ('rolling', 'ewma'):
        selected = [r for r in attempts if r['model'] == model and r['status'] == 'assumed_filled_scored']
        models[model] = {'attempts': sum(r['model'] == model for r in attempts), 'scored_positions': len(selected),
                         'net_profit_dollars': str(sum((decimal(r['net_profit']) for r in selected), Decimal(0))),
                         'fees_dollars': str(sum((decimal(r['execution_fee']) for r in selected), Decimal(0)))}
    report = {'schema_version': 1, 'package_version': __version__, 'status': 'completed', 'settings': settings,
              'shadow_report_sha256': digest(directory / 'report.json'), 'policy_sha256': digest(Path(policy_path)),
              'label_sources': label_sources, 'counts': dict(counts), 'models': models,
              'interpretation': 'Hypothetical FOK taker entry and hold-to-settlement scenarios using later REST snapshots. Displayed size is not a guaranteed fill. Each model is a separate strategy; no combined portfolio or live orders.'}
    output.mkdir(parents=True, exist_ok=False)
    if attempts:
        _csv(output / 'attempts.csv', attempts)
        report['artifacts'] = {'attempts.csv': digest(output / 'attempts.csv')}
    else:
        report['artifacts'] = {}
    save_json(output / 'report.json', report)
    return report
