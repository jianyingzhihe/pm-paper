"""连接公开资料、角色交付、冻结决定、执行和已有持仓复核。"""
from pathlib import Path
import uuid

from .account import PaperAccount, validate_decision, _hash
from .agents import create_agent
from .execution import preflight
from .files import now, read_json, write_json, workspace_lock
from .public import PublicClient, as_list
from .screening import screen_catalog


def market_item(market, outcome, action="WATCH"):
    outcomes = as_list(market['outcomes'])
    tokens = as_list(market['clobTokenIds'])
    if len(outcomes) != len(tokens) or outcomes.count(outcome) != 1:
        raise ValueError("市场结果与代币对应不唯一")
    event = market.get('event') or {}
    events = market.get('events') or []
    eid = event.get('id') or market.get('event_id')
    if eid is None and len(events) == 1:
        eid = events[0].get('id')
    if eid is None:
        raise ValueError("事件归属未知")
    rules = {key: market.get(key) for key in ['description', 'resolutionSource', 'endDate', 'outcomes']}
    return {'market_id': str(market['id']), 'event_id': str(eid), 'title': market['question'],
            'condition_id': market['conditionId'], 'token_id': str(tokens[outcomes.index(outcome)]),
            'outcome': outcome, 'action': action, 'rules_text': market.get('description'), 'rules_hash': _hash(rules)}


def build_decision(draft, markets, policy_version):
    items = []
    invalid = []
    proposals = draft.get('items', [])
    if not isinstance(proposals, list):
        raise ValueError("逐项决定需要数组")
    for proposal in proposals:
        try:
            market = markets[str(proposal['market_id'])]
            item = market_item(market, proposal['outcome'], proposal['action'])
            for field in ['reason', 'support', 'counterevidence', 'sources', 'probability']:
                item[field] = proposal[field]
            if any(not isinstance(item[field], str) or not item[field].strip() for field in ['reason', 'support', 'counterevidence']):
                raise ValueError("理由、支持和反证需要明确文本，未知项按实际说明")
            if not isinstance(item['sources'], list):
                raise ValueError("资料引用需要数组")
            for field in ['budget', 'quantity', 'limit_price', 'probability_method', 'invalidation', 'next_review']:
                if field in proposal:
                    item[field] = proposal[field]
            if item['probability'] is not None and not item.get('probability_method'):
                raise ValueError("数值概率缺可追溯方法说明")
            probe = {'decision_id': 'validation', 'created_at': now(), 'policy_version': policy_version, 'items': [item]}
            validate_decision(probe)
            items.append(item)
        except (ValueError, KeyError, TypeError, ArithmeticError) as error:
            invalid.append({'proposal': proposal, 'error': str(error)})
    decision = {'decision_id': uuid.uuid4().hex, 'created_at': now(), 'policy_version': policy_version, 'items': items}
    validate_decision(decision)
    return decision, invalid


def run_cycle(config, catalog, workspace, *, account_path=None, execute=False, evidence=None, agent=None, client=None):
    with workspace_lock(workspace):
        return _run_cycle(config, catalog, workspace, account_path=account_path, execute=execute,
                          evidence=evidence, agent=agent, client=client)


def _run_cycle(config, catalog, workspace, *, account_path=None, execute=False, evidence=None, agent=None, client=None):
    if execute and account_path is None:
        raise ValueError("执行需要明确指定已有账户文件")
    workspace = Path(workspace)
    directory = workspace / 'rounds' / uuid.uuid4().hex
    write_json(directory / 'config.json', config)
    record = {'started_at': now(), 'directory': str(directory), 'success': False, 'execute_requested': execute}
    client = client or PublicClient(**config.get('public', {}))
    account = None
    try:
        if account_path is not None:
            account = PaperAccount(account_path)
            owned = {pos['market_id'] for pos in account.state()['positions'].values()}
            if owned:
                existing = client.snapshot(sorted(owned))
                write_json(directory / 'existing_snapshot.json', existing)
                write_json(directory / 'existing_review.json', account.review(existing))
        history_path = workspace / 'selection_history.json'
        history = read_json(history_path) if history_path.exists() else {}
        screen = screen_catalog(catalog, config['screening'], history)
        write_json(directory / 'screening.json', screen)
        count = config.get('events_per_cycle', 3)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("每轮事件数需要非负整数，零用于只复核已有事项")
        selected = screen['candidates'][:count]
        if not selected:
            record.update(success=True, status='no_research_candidates')
            return record
        for group in selected:
            history[group['event_id']] = now()
        write_json(history_path, history)
        selected_markets = [dict(group['representative']['market'], event_id=group['event_id']) for group in selected]
        analysis = client.snapshot([market['id'] for market in selected_markets])
        write_json(directory / 'analysis_snapshot.json', analysis)
        markets = {}
        checks = []
        for original in selected_markets:
            mid = str(original['id'])
            market = analysis.get('markets', {}).get(mid, {}).get('gamma') or original
            market = dict(market, event=original.get('event'), event_id=original.get('event_id'))
            markets[mid] = market
            for outcome in as_list(market.get('outcomes')):
                item = market_item(market, outcome, 'BUY')
                item.update(budget=config.get('reference_budget', '5'), limit_price='0.999999')
                checks.append(preflight(item, analysis, account.state()['cash'] if account else None))
        fields = ['id', 'question', 'conditionId', 'description', 'resolutionSource', 'endDate', 'outcomes', 'clobTokenIds']
        model_markets = []
        for market in markets.values():
            model_market = {key: market.get(key) for key in fields}
            model_market['event_id'] = market['event_id']
            model_markets.append(model_market)
        context = {'markets': model_markets, 'preflight': checks, 'evidence': evidence or [],
                   'account': account.state() if account else None, 'catalog_complete': catalog.get('complete', False),
                   'analysis_quote_fetched_at': analysis.get('fetched_at')}

        def call(role, task, data, stage):
            settings = dict(config['agent'], **config.get('role_overrides', {}).get(role, {}))
            selected_agent = agent or create_agent(settings)
            return selected_agent.run(role, task, data, directory / 'agents' / stage)

        draft = call('researcher', '评估所给事件，提交逐项动作及来源缺口。', context, 'research')
        reviews = []
        for index, role in enumerate(config.get('review_roles', [])):
            reviews.append(call(role, '核对所给判断的具体错误与反证。', dict(context, draft=draft), 'review_' + str(index)))
        if reviews:
            draft = call('coordinator', '核验各条意见并形成最终逐项决定，保存意见处理。', dict(context, draft=draft, reviews=reviews), 'final')
        decision, invalid = build_decision(draft, markets, config['policy_version'])
        write_json(directory / 'decision.json', decision)
        write_json(directory / 'invalid_items.json', invalid)
        record.update(status='decision_saved', decision_id=decision['decision_id'], item_count=len(decision['items']),
                      invalid_item_count=len(invalid), selected_event_ids=[group['event_id'] for group in selected])
        record['uncovered_market_ids'] = sorted(set(markets) - {item['market_id'] for item in decision['items']})
        if execute:
            write_json(directory / 'freeze_receipt.json', account.freeze(decision))
            fresh = client.snapshot([item['market_id'] for item in decision['items']])
            write_json(directory / 'execution_snapshot.json', fresh)
            write_json(directory / 'execution.json', account.execute(decision['decision_id'], fresh))
            record['status'] = 'execution_recorded'
        record['success'] = True
        return record
    except Exception as error:
        record['error'] = str(error)
        raise
    finally:
        if account is not None:
            try:
                ids = sorted({pos['market_id'] for pos in account.state()['positions'].values()})
                final_snapshot = client.snapshot(ids) if ids else {'started_at': now(), 'fetched_at': now(), 'markets': {}, 'books': {}, 'fees': {}}
                write_json(directory / 'final_snapshot.json', final_snapshot)
                write_json(directory / 'final_review.json', account.review(final_snapshot))
                write_json(directory / 'account_report.json', account.report(final_snapshot))
            except Exception as error:
                record['final_review_error'] = str(error)
                write_json(directory / 'account_report.json', account.report({}))
            finally:
                account.close()
        record['finished_at'] = now()
        write_json(directory / 'manifest.json', record)
        write_json(workspace / 'latest_round.json', record)
