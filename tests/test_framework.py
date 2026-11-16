import contextlib
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from pm_paper.account import PaperAccount
from pm_paper.agents import CompatibleAgent, CommandAgent
from pm_paper.cli import main
from pm_paper.demo import synthetic_item, synthetic_snapshot
from pm_paper.execution import preflight
from pm_paper.files import now, read_json, workspace_lock
from pm_paper.fills import final_winner
from pm_paper.public import PublicClient
from pm_paper.screening import screen_catalog
from pm_paper.workflow import run_cycle, build_decision


def config():
    return read_json(Path(__file__).parents[1] / 'pm_paper/default_config.json')


def market():
    value = synthetic_snapshot()['markets']['1']['gamma']
    value.update(endDate=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat(),
                 event={'id': 'example-event', 'title': '合成事件'}, enableOrderBook=True,
                 outcomePrices=['0.5', '0.5'], bestBid='0.4', bestAsk='0.5', resolutionSource='https://example.org/source')
    return value


def proposal():
    return {'market_id': '1', 'outcome': 'Yes', 'action': 'BUY', 'budget': '5', 'limit_price': '0.6',
            'reason': '合成软件检查', 'support': '已提供合成资料', 'counterevidence': '可能错误',
            'sources': [], 'probability': None}


class Handler(BaseHTTPRequestHandler):
    roles = []

    def log_message(self, *args):
        pass

    def send_json(self, value):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(value).encode())

    def do_GET(self):
        if self.path.startswith('/events/keyset'):
            self.send_json({'events': [{'id': 'example-event', 'title': '合成事件', 'markets': [market()]}]})
        elif self.path == '/markets/1':
            raw = market()
            raw.pop('events', None)
            raw.pop('event', None)
            self.send_json(raw)
        elif self.path == '/markets/example-condition':
            self.send_json(synthetic_snapshot()['markets']['1']['clob'])
        elif self.path == '/clob-markets/example-condition':
            self.send_json(synthetic_snapshot()['fees']['1'])
        else:
            self.send_error(404)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/books':
            books = synthetic_snapshot()['books']
            self.send_json([books[row['token_id']] for row in request if row['token_id'] in books])
        elif self.path == '/chat/completions':
            role = json.loads(request['messages'][1]['content'])['role']
            type(self).roles.append(role)
            answer = {'issues': []} if role == 'critic' else {'items': [proposal()]}
            self.send_json({'id': 'local-test', 'model': request['model'], 'usage': {},
                            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(answer)}}]})
        else:
            self.send_error(404)


class FrameworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def settings(self):
        value = config()
        value['public'].update(gamma_base=self.url, clob_base=self.url, retries=0)
        value['agent'].update(base_url=self.url, model='test-model', api_key_env='PM_PAPER_TEST_KEY')
        return value

    def test_real_http_adapter_and_full_cycle(self):
        settings = self.settings()
        client = PublicClient(**settings['public'])
        catalog = client.catalog(1)
        self.assertTrue(catalog['complete'])
        with TemporaryDirectory() as directory, patch.dict(os.environ, {'PM_PAPER_TEST_KEY': 'synthetic-test-key'}):
            path = Path(directory) / 'account.sqlite'
            account = PaperAccount(path, create=True)
            account.close()
            Handler.roles = []
            result = run_cycle(settings, catalog, Path(directory) / 'work', account_path=path, execute=True)
            self.assertTrue(result['success'])
            self.assertEqual(Handler.roles, ['researcher', 'critic', 'coordinator'])
            execution = read_json(Path(result['directory']) / 'execution.json')
            self.assertEqual(execution['items'][0]['status'], 'filled')
            report = read_json(Path(result['directory']) / 'account_report.json')
            self.assertEqual(report['buy_count'], 1)
            self.assertEqual(report['initial_cash'], '1000')
            for path in (Path(directory) / 'work').rglob('*.json'):
                self.assertNotIn('synthetic-test-key', path.read_text(encoding='utf-8'))

    def test_agent_failure_still_reviews_existing_positions(self):
        settings = self.settings()
        client = PublicClient(**settings['public'])
        class BrokenAgent:
            def run(self, *args):
                raise RuntimeError('合成接口失败')
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'account.sqlite'
            account = PaperAccount(path, create=True)
            account.freeze({'decision_id': 'buy', 'created_at': now(), 'policy_version': 'test', 'items': [synthetic_item()]})
            account.execute('buy', synthetic_snapshot())
            account.close()
            work = Path(directory) / 'work'
            with self.assertRaisesRegex(RuntimeError, '合成接口失败'):
                run_cycle(settings, client.catalog(1), work, account_path=path, agent=BrokenAgent())
            result = read_json(work / 'latest_round.json')
            self.assertFalse(result['success'])
            self.assertTrue((Path(result['directory']) / 'final_review.json').is_file())
            self.assertEqual(read_json(Path(result['directory']) / 'account_report.json')['buy_count'], 1)

    def test_command_agent_and_duplicate_request(self):
        command = [sys.executable, '-c', 'import json,sys; request=json.load(sys.stdin); print(json.dumps({"role": request["role"]}))']
        with TemporaryDirectory() as directory:
            agent = CommandAgent({'command': command})
            self.assertEqual(agent.run('collector', '合成任务', {}, directory), {'role': 'collector'})
            with self.assertRaises(ValueError):
                agent.run('collector', '合成任务', {}, directory)

    def test_missing_key_and_truncated_model_output(self):
        settings = self.settings()['agent']
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                CompatibleAgent(settings).run('researcher', 'test', {}, directory)
        response = {'choices': [{'finish_reason': 'length', 'message': {'content': '{"items":'}}]}
        raw = io.BytesIO(json.dumps(response).encode())
        with TemporaryDirectory() as directory, patch.dict(os.environ, {'PM_PAPER_TEST_KEY': 'test-key'}):
            with patch('pm_paper.agents.urlopen', return_value=raw):
                with self.assertRaises(RuntimeError):
                    CompatibleAgent(settings).run('researcher', 'test', {}, directory)
            self.assertTrue((Path(directory) / 'error.json').is_file())
            self.assertFalse((Path(directory) / 'result.json').exists())

    def test_preflight_unknown_fees_and_actual_quantity(self):
        quote = synthetic_snapshot()
        quote['books']['example-yes']['asks'][0]['size'] = '3'
        quote['books']['example-yes']['bids'][0]['size'] = '1'
        result = preflight(synthetic_item(), quote)
        self.assertEqual(result['filled'], '3.00')
        self.assertEqual(result['unable_to_exit'], '2.00')
        quote['fees'] = {}
        self.assertEqual(preflight(synthetic_item(), quote)['status'], 'blocked')

    def test_screening_soft_flags_extremes_and_stale_catalog(self):
        value = market()
        value['liquidityNum'] = '0'
        catalog = {'fetched_at': now(), 'complete': False, 'markets': [value]}
        result = screen_catalog(catalog, config()['screening'])
        self.assertEqual(len(result['candidates']), 1)
        self.assertFalse(result['catalog_complete'])
        value['outcomePrices'] = ['0.01', '0.99']
        self.assertEqual(screen_catalog(catalog, config()['screening'])['routes']['专项研究'], 1)
        catalog['fetched_at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=37)).isoformat()
        with self.assertRaises(ValueError):
            screen_catalog(catalog, config()['screening'])

    def test_account_loading_needs_explicit_creation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.sqlite'
            with self.assertRaises(FileNotFoundError):
                PaperAccount(path)
            self.assertFalse(path.exists())

    def test_cli_demo_and_missing_account(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['demo']), 0)
        self.assertEqual(json.loads(output.getvalue())['data_type'], 'synthetic')
        with TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['account', 'report', '--path', str(Path(directory) / 'missing.sqlite')]), 1)

    def test_formal_sources_must_agree(self):
        snapshot = synthetic_snapshot()
        state = snapshot['markets']['1']
        state['gamma'].update(closed=True, umaResolutionStatus='resolved', outcomePrices=['0', '1'])
        state['clob']['closed'] = True
        state['clob']['tokens'][0].update(winner=True, price='1')
        state['clob']['tokens'][1].update(winner=False, price='0')
        self.assertIsNone(final_winner(state))
        state['gamma']['outcomePrices'] = ['1', '0']
        self.assertEqual(final_winner(state), 'Yes')
        state['gamma'].pop('outcomePrices')
        self.assertIsNone(final_winner(state))
        state['gamma']['outcomePrices'] = ['1', '0']
        state['clob']['tokens'][0]['winner'] = 'true'
        self.assertIsNone(final_winner(state))

    def test_bad_config_returns_structured_cli_error(self):
        with TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()) as output:
            result = main(['--config', str(Path(directory) / 'missing.json'), 'demo'])
        self.assertEqual(result, 1)
        self.assertIn('error', json.loads(output.getvalue()))

    def test_remote_public_transport_keeps_payload_out_of_shell(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps({'data': {'value': 1}, 'http_status': 200}), '')
        with patch.dict(os.environ, {'TEST_PUBLIC_HOST': 'example-host'}):
            with patch('pm_paper.public.subprocess.run', return_value=completed) as called:
                client = PublicClient(ssh_host_env='TEST_PUBLIC_HOST')
                self.assertEqual(client.request('GET', 'https://example.org/data'), {'value': 1})
                args = called.call_args
                self.assertEqual(args.args[0][0], 'ssh')
                self.assertNotIn('https://example.org/data', args.args[0][-1])
                self.assertEqual(json.loads(args.kwargs['input'])['url'], 'https://example.org/data')

    def test_malformed_items_and_negative_cycle_size(self):
        with self.assertRaisesRegex(ValueError, '逐项决定需要数组'):
            build_decision({'items': None}, {}, 'test')
        settings = self.settings()
        settings['events_per_cycle'] = -1
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, '每轮事件数需要非负整数'):
                run_cycle(settings, {'fetched_at': now(), 'markets': [market()]}, directory)

    def test_partial_catalog_keeps_pagination_boundary(self):
        client = PublicClient(retries=0)
        with patch.object(client, 'request', return_value={'events': [], 'next_cursor': 'next-page'}):
            catalog = client.catalog(max_pages=1)
        self.assertFalse(catalog['complete'])
        self.assertEqual(catalog['next_cursor'], 'next-page')

    def test_project_cycle_lock_is_exclusive_and_released(self):
        with TemporaryDirectory() as directory:
            with workspace_lock(directory):
                with self.assertRaisesRegex(RuntimeError, '已有周期运行'):
                    run_cycle(self.settings(), {}, directory)
            with workspace_lock(directory):
                pass


if __name__ == '__main__':
    unittest.main()
