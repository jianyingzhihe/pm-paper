"""用动态生成的合成资料验证完整执行路径。"""
import copy
import tempfile
from pathlib import Path

from .account import PaperAccount
from .files import now


def synthetic_item(action='BUY'):
    return {'market_id': '1', 'event_id': 'example-event', 'title': '合成示例事件',
            'condition_id': 'example-condition', 'token_id': 'example-yes', 'outcome': 'Yes',
            'action': action, 'budget': '5', 'quantity': '2', 'limit_price': '0.6' if action == 'BUY' else '0.3',
            'reason': '软件执行路径验证', 'support': '合成输入', 'counterevidence': '合成输入',
            'probability': None, 'sources': [], 'rules_text': '合成完整规则', 'rules_hash': 'example-rules'}


def synthetic_snapshot():
    return {'started_at': now(), 'fetched_at': now(), 'markets': {'1': {
        'gamma': {'id': '1', 'conditionId': 'example-condition', 'question': '合成示例事件',
                  'description': '合成完整规则', 'active': True, 'closed': False, 'acceptingOrders': True,
                  'outcomes': ['Yes', 'No'], 'outcomePrices': ['0.5', '0.5'], 'clobTokenIds': ['example-yes', 'example-no'],
                  'events': [{'id': 'example-event'}]},
        'clob': {'condition_id': 'example-condition', 'closed': False, 'accepting_orders': True,
                 'tokens': [{'token_id': 'example-yes', 'outcome': 'Yes', 'winner': False, 'price': '0.5'},
                            {'token_id': 'example-no', 'outcome': 'No', 'winner': False, 'price': '0.5'}]}}},
        'books': {'example-yes': {'asset_id': 'example-yes', 'market': 'example-condition', 'timestamp': now(),
                                  'min_order_size': '1', 'asks': [{'price': '0.5', 'size': '100'}],
                                  'bids': [{'price': '0.4', 'size': '100'}]}},
        'fees': {'1': {'c': 'example-condition', 't': [{'t': 'example-yes', 'o': 'Yes'}, {'t': 'example-no', 'o': 'No'}],
                       'fd': {'r': '0.02', 'e': '1'}}}}


def run_demo():
    with tempfile.TemporaryDirectory() as directory:
        account = PaperAccount(Path(directory) / 'example.sqlite', create=True)
        try:
            buy = {'decision_id': 'example-buy', 'created_at': now(), 'policy_version': 'synthetic-demo', 'items': [synthetic_item()]}
            account.freeze(buy)
            quote = synthetic_snapshot()
            bought = account.execute(buy['decision_id'], quote)
            sold_decision = {'decision_id': 'example-sell', 'created_at': now(), 'policy_version': 'synthetic-demo', 'items': [synthetic_item('SELL')]}
            account.freeze(sold_decision)
            sold = account.execute(sold_decision['decision_id'], synthetic_snapshot())
            settled_quote = copy.deepcopy(synthetic_snapshot())
            settled_quote['markets']['1']['gamma'].update(closed=True, umaResolutionStatus='resolved', outcomePrices=['1', '0'])
            settled_quote['markets']['1']['clob']['closed'] = True
            settled_quote['markets']['1']['clob']['tokens'][0].update(winner=True, price='1')
            settled_quote['markets']['1']['clob']['tokens'][1].update(winner=False, price='0')
            settlement = account.review(settled_quote)
            return {'data_type': 'synthetic', 'buy': bought, 'sell': sold, 'settlement': settlement,
                    'report': account.report(settled_quote)}
        finally:
            account.close()
