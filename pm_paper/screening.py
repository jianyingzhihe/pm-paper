"""按目录参考价格、资料条件及反馈时间安排事件研究。"""
import datetime as dt
from decimal import Decimal
import json
from .account import _hash
from .files import now

UTC = dt.timezone.utc


def moment(value):
    parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('时间需要明确时区')
    return parsed.astimezone(UTC)


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except Exception:
        return None


def array(value):
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def price_band(cost, config):
    value = number(cost)
    if value is None or not 0 < value < 1:
        return '待核价'
    if Decimal(config['优先含费成本下限']) <= value <= Decimal(config['优先含费成本上限']):
        return '优先'
    if Decimal(config['常规含费成本下限']) <= value <= Decimal(config['常规含费成本上限']):
        return '普通'
    return '专项'


def classify(market, config, at):
    flags = []
    inactive = any(market.get(key) is True for key in ['closed', 'archived'])
    inactive = inactive or any(market.get(key) is False for key in ['active', 'acceptingOrders', 'enableOrderBook'])
    unknown_state = any(market.get(key) is None for key in ['active', 'closed', 'acceptingOrders', 'enableOrderBook'])
    rules = bool(str(market.get('description') or '').strip())
    source = market.get('resolutionSource')
    if not rules:
        flags.append('缺规则正文')
    if not source:
        flags.append('指定来源待正文核对')
    remaining = None
    try:
        remaining = (moment(market.get('endDate')) - at).total_seconds() / 86400
        if remaining <= 0:
            flags.append('目录截止已过，正文及延期待复核')
    except ValueError:
        flags.append('目录截止未知')
    bid, ask = number(market.get('bestBid')), number(market.get('bestAsk'))
    if bid is None or ask is None or not 0 < bid < 1 or not 0 < ask < 1:
        flags.append('目录缺有效两面参考，待真实盘口')
    elif bid > ask:
        flags.append('目录参考交叉，待真实盘口')
    elif ask - bid > Decimal(config['目录价差排序参考']):
        flags.append('参考价差较宽')
    liquidity = number(market.get('liquidityNum'))
    volume = number(market.get('volume24hr'))
    if liquidity is not None and liquidity < Decimal(config['目录流动性排序参考']):
        flags.append('目录流动性较低')
    if volume is not None and volume < Decimal(config['近24小时成交排序参考']):
        flags.append('成交参考较少')
    prices = [number(item) for item in array(market.get('outcomePrices'))]
    bands = [price_band(value, config) for value in prices]
    band = min(bands, key=lambda item: ['优先', '普通', '专项', '待核价'].index(item)) if bands else '待核价'
    if inactive:
        route = '当前状态暂停新入场'
    elif not rules or unknown_state or remaining is None or remaining <= 0:
        route = '待补查'
    elif band == '专项':
        route = '专项研究'
    elif band == '待核价' or any('待真实盘口' in flag for flag in flags):
        route = '待盘口核验'
    else:
        route = '常规研究候选'
    return {'去向': route, '目录参考价格档': band, '标记': flags, '目录剩余天数': remaining,
            '来源线索存在': bool(source), '近期反馈线索': remaining is not None and 1 <= remaining <= 7,
            '近24小时成交参考': str(volume) if volume is not None else None,
            '价格依据': '目录各选项参考值，真实含费买价由盘口核验确定'}


def screen_catalog(catalog, config, history=None):
    captured = moment(catalog['fetched_at'])
    at = moment(now())
    if not 0 <= (at - captured).total_seconds() <= 36 * 3600:
        raise ValueError('目录时间不合格，继续已有持仓及正式结果复核')
    groups = {}
    counts = {}
    for market in catalog['markets']:
        checked = classify(market, config, at)
        counts[checked['去向']] = counts.get(checked['去向'], 0) + 1
        event = market.get('event') or {}
        eid = str(event.get('id') or market.get('event_id') or '')
        if not eid:
            counts['归属待核'] = counts.get('归属待核', 0) + 1
            continue
        row = {'market': market, 'screening': checked}
        group = groups.setdefault(eid, {'event_id': eid, 'title': event.get('title'), 'markets': []})
        group['markets'].append(row)
    candidates = []
    for group in groups.values():
        eligible = [row for row in group['markets'] if row['screening']['去向'] in ['常规研究候选', '待盘口核验']]
        eligible.sort(key=lambda row: (row['screening']['目录参考价格档'] != '优先',
                                       not row['screening']['近期反馈线索'], len(row['screening']['标记'])))
        group['representative'] = eligible[0] if eligible else None
        if eligible:
            candidates.append(group)
    history = history or {}
    candidates.sort(key=lambda group: (history.get(group['event_id'], ''),
                                       group['representative']['screening']['目录参考价格档'] != '优先',
                                       not group['representative']['screening']['近期反馈线索'],
                                       len(group['representative']['screening']['标记']), group['event_id']))
    return {'created_at': now(), 'catalog_hash': _hash(catalog), 'catalog_complete': catalog.get('complete', False),
            'market_count': len(catalog['markets']), 'event_count': len(groups), 'routes': counts,
            'events': list(groups.values()), 'candidates': candidates}
