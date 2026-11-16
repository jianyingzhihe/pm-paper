"""模型研究、公开盘口、预检和模拟账本的命令行入口。"""
import argparse
import json
from pathlib import Path
import sys
import sqlite3

from .account import PaperAccount
from .agents import create_agent
from .execution import preflight
from .files import now, read_json, write_json
from .public import PublicClient
from .screening import screen_catalog
from .workflow import run_cycle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(Path(__file__).with_name('default_config.json')))
    commands = parser.add_subparsers(dest='command', required=True)
    catalog = commands.add_parser('catalog', help='读取事件及所属市场，保存分页范围')
    catalog.add_argument('--max-pages', type=int, default=20)
    catalog.add_argument('--page-size', type=int, default=100)
    catalog.add_argument('--cursor')
    catalog.add_argument('--output', required=True)
    snapshot = commands.add_parser('snapshot', help='读取指定市场实际两面盘口和费用')
    snapshot.add_argument('--market', action='append', required=True)
    snapshot.add_argument('--output', required=True)
    screen = commands.add_parser('screen', help='按事件整理目录初筛与候选')
    screen.add_argument('--catalog', required=True)
    screen.add_argument('--output', required=True)
    check = commands.add_parser('preflight', help='核算买入及退出，供任何账本调用')
    check.add_argument('--item', required=True)
    check.add_argument('--snapshot', required=True)
    check.add_argument('--cash')
    check.add_argument('--output', required=True)
    agent = commands.add_parser('agent', help='调用一个可配置角色')
    agent.add_argument('--role', choices=['project_start', 'researcher', 'collector', 'critic', 'coordinator', 'retrospective'], required=True)
    agent.add_argument('--context', required=True)
    agent.add_argument('--task', required=True)
    agent.add_argument('--output-dir', required=True)
    cycle = commands.add_parser('cycle', help='研究、意见核验及可选模拟执行')
    cycle.add_argument('--catalog', required=True)
    cycle.add_argument('--workspace', required=True)
    cycle.add_argument('--evidence')
    cycle.add_argument('--account')
    cycle.add_argument('--execute', action='store_true')
    account = commands.add_parser('account', help='操作明确指定的兼容模拟账本')
    account.add_argument('operation', choices=['create', 'freeze', 'execute', 'review', 'report', 'journal'])
    account.add_argument('--path', required=True)
    account.add_argument('--initial-cash', default='1000')
    account.add_argument('--decision')
    account.add_argument('--decision-id')
    account.add_argument('--snapshot')
    account.add_argument('--output')
    commands.add_parser('demo', help='在临时目录跑通合成买入、卖出及结算')
    args = parser.parse_args(argv)
    client = None
    try:
        config = read_json(args.config)
        client = PublicClient(**config.get('public', {}))
        if args.command == 'catalog':
            result = client.catalog(args.max_pages, args.page_size, args.cursor)
        elif args.command == 'snapshot':
            result = client.snapshot(args.market)
        elif args.command == 'screen':
            result = screen_catalog(read_json(args.catalog), config['screening'])
        elif args.command == 'preflight':
            result = preflight(read_json(args.item), read_json(args.snapshot), args.cash)
        elif args.command == 'agent':
            settings = dict(config['agent'], **config.get('role_overrides', {}).get(args.role, {}))
            result = create_agent(settings).run(args.role, args.task, read_json(args.context), args.output_dir)
        elif args.command == 'cycle':
            result = run_cycle(config, read_json(args.catalog), args.workspace, account_path=args.account,
                               execute=args.execute, evidence=read_json(args.evidence) if args.evidence else None)
        elif args.command == 'demo':
            from .demo import run_demo
            result = run_demo()
        else:
            result = account_command(args, client)
        if getattr(args, 'output', None):
            write_json(args.output, result)
            print(json.dumps({'output': args.output, 'saved_at': now()}, ensure_ascii=False))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error) as error:
        receipt = {'error': str(error), 'at': now(), 'public_requests': client.requests if client else []}
        if getattr(args, 'output', None):
            write_json(args.output + '.error.json', receipt)
        print(json.dumps(receipt, ensure_ascii=False), file=sys.stderr)
        return 1


def account_command(args, client):
    if args.operation == 'create' and Path(args.path).exists():
        raise ValueError('目标账户已存在，读取原账户或选择明确的新文件')
    account = PaperAccount(args.path, create=args.operation == 'create', initial_cash=args.initial_cash)
    try:
        if args.operation == 'create':
            return account.state()
        if args.operation == 'freeze':
            if not args.decision:
                raise ValueError('冻结需要决定文件')
            return account.freeze(read_json(args.decision))
        if args.operation == 'journal':
            rows = account.db.execute('SELECT * FROM journal ORDER BY seq').fetchall()
            return [{'seq': row[0], 'at': row[1], 'kind': row[2], 'payload': json.loads(row[3])} for row in rows]
        if args.operation == 'execute':
            if not args.decision_id:
                raise ValueError('执行需要已冻结的决定标识')
            existing = account.execution(args.decision_id)
            if existing is not None:
                return existing
            frozen = account.db.execute('SELECT payload FROM decisions WHERE decision_id=?', (args.decision_id,)).fetchone()
            if frozen is None:
                raise ValueError('决定尚未冻结')
            identifiers = [item['market_id'] for item in json.loads(frozen[0])['items']]
        else:
            identifiers = [pos['market_id'] for pos in account.state()['positions'].values()]
        snapshot = read_json(args.snapshot) if args.snapshot else client.snapshot(identifiers)
        if args.operation == 'execute':
            return account.execute(args.decision_id, snapshot)
        if args.operation == 'review':
            return account.review(snapshot)
        return account.report(snapshot)
    finally:
        account.close()


if __name__ == '__main__':
    raise SystemExit(main())
