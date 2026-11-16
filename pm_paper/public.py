"""读取公开市场、费用和订单簿，保留分页范围及逐次请求。"""
import json
import os
import re
import shlex
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .files import now


REMOTE_HTTP = '''import json,sys
from urllib.request import Request,urlopen
value=json.load(sys.stdin)
body=json.dumps(value["payload"]).encode() if value["payload"] is not None else None
request=Request(value["url"],data=body,method=value["method"],headers={"User-Agent":"pm-paper/0.1","Content-Type":"application/json"})
try:
    with urlopen(request,timeout=value["timeout"]) as response:
        print(json.dumps({"data":json.load(response),"http_status":response.status,"response_date":response.headers.get("Date")}))
except Exception as error:
    print(json.dumps({"error":str(error),"http_status":getattr(error,"code",None)}))
'''


def as_list(value):
    return json.loads(value) if isinstance(value, str) else value or []


class PublicClient:
    def __init__(self, gamma_base="https://gamma-api.polymarket.com", clob_base="https://clob.polymarket.com", timeout=20, retries=1, ssh_host_env=None):
        if timeout <= 0 or isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            raise ValueError("公开读取超时必须为正数，重试次数必须为非负整数")
        self.gamma_base = gamma_base.rstrip("/")
        self.clob_base = clob_base.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.ssh_host_env = ssh_host_env
        self.requests = []

    def request(self, method, url, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        for attempt in range(self.retries + 1):
            entry = {"started_at": now(), "method": method, "url": url, "attempt": attempt + 1}
            if payload is not None:
                entry["request_body"] = payload
            try:
                host = os.environ.get(self.ssh_host_env) if self.ssh_host_env else None
                if host:
                    if host.startswith('-') or not re.fullmatch(r'[A-Za-z0-9_.@:-]+', host):
                        raise ValueError("公开资料主机别名不合法")
                    command = ['ssh', '-oBatchMode=yes', '-oConnectTimeout=10', host, 'python3 -c ' + shlex.quote(REMOTE_HTTP)]
                    sent = {"method": method, "url": url, "payload": payload, "timeout": self.timeout}
                    completed = subprocess.run(command, input=json.dumps(sent), text=True, encoding='utf-8',
                                               capture_output=True, timeout=self.timeout + 15)
                    if completed.returncode:
                        raise URLError('公开资料主机读取失败 ' + completed.stderr[-1000:])
                    received = json.loads(completed.stdout)
                    entry.update(http_status=received.get('http_status'), response_date=received.get('response_date'))
                    if 'error' in received:
                        if received.get('http_status'):
                            raise HTTPError(url, received['http_status'], received['error'], None, None)
                        raise URLError(received['error'])
                    result = received['data']
                else:
                    request = Request(url, data=body, method=method, headers={"User-Agent": "pm-paper/0.1", "Content-Type": "application/json"})
                    with urlopen(request, timeout=self.timeout) as response:
                        entry["http_status"] = response.status
                        entry["response_date"] = response.headers.get("Date")
                        result = json.load(response)
                entry["status"] = "success"
                return result
            except (HTTPError, URLError, TimeoutError, ValueError, subprocess.TimeoutExpired) as error:
                entry["status"] = "failed"
                entry["error"] = str(error)
                retryable = isinstance(error, (URLError, TimeoutError, subprocess.TimeoutExpired)) and not isinstance(error, HTTPError)
                retryable = retryable or isinstance(error, HTTPError) and error.code in {429, 500, 502, 503, 504}
                if not retryable or attempt == self.retries:
                    raise
                time.sleep(min(2 ** attempt, 8))
            finally:
                entry["finished_at"] = now()
                self.requests.append(entry)

    def snapshot(self, market_ids):
        start_index = len(self.requests)
        result = {"started_at": now(), "markets": {}, "books": {}, "fees": {}, "errors": []}
        tokens = set()
        for mid in dict.fromkeys(str(value) for value in market_ids):
            try:
                gamma = self.request("GET", self.gamma_base + "/markets/" + mid)
                condition = gamma["conditionId"]
                state = {"gamma": gamma, "clob": {}}
                result["markets"][mid] = state
                try:
                    state["clob"] = self.request("GET", self.clob_base + "/markets/" + condition)
                except Exception as error:
                    result["errors"].append({"market_id": mid, "component": "market_status", "error": str(error)})
                try:
                    result["fees"][mid] = self.request("GET", self.clob_base + "/clob-markets/" + condition)
                except Exception as error:
                    result["errors"].append({"market_id": mid, "component": "fees", "error": str(error)})
                if not gamma.get("closed"):
                    tokens.update(str(value) for value in as_list(gamma.get("clobTokenIds")))
            except Exception as error:
                result["errors"].append({"market_id": mid, "component": "gamma", "error": str(error)})
        ordered = sorted(tokens)
        for offset in range(0, len(ordered), 50):
            batch = ordered[offset:offset + 50]
            try:
                books = self.request("POST", self.clob_base + "/books", [{"token_id": value} for value in batch])
                for book in books:
                    result["books"][str(book["asset_id"])] = book
            except Exception as error:
                result["errors"].append({"token_ids": batch, "component": "books", "error": str(error)})
        result["fetched_at"] = now()
        result["requests"] = self.requests[start_index:]
        return result

    def catalog(self, max_pages=20, page_size=100, cursor=None):
        if max_pages < 1 or not 1 <= page_size <= 100:
            raise ValueError("页数必须为正数，每页1至100个事件")
        start_index = len(self.requests)
        result = {"started_at": now(), "events": [], "markets": [], "pages": [], "complete": False,
                  "start_cursor": cursor, "scope": "未关闭事件及这些事件中的未关闭市场"}
        seen = set()
        for page in range(max_pages):
            params = {"closed": "false", "limit": page_size, "order": "id", "ascending": "true"}
            if cursor:
                params["after_cursor"] = cursor
            data = self.request("GET", self.gamma_base + "/events/keyset?" + urlencode(params))
            if not isinstance(data, dict) or not isinstance(data.get("events"), list):
                raise ValueError("事件分页返回结构异常")
            for event in data["events"]:
                event_info = {key: value for key, value in event.items() if key != "markets"}
                result["events"].append(event_info)
                for market in event.get("markets", []):
                    if not market.get("closed"):
                        result["markets"].append(dict(market, event=event_info))
            cursor = data.get("next_cursor")
            result["pages"].append({"page": page + 1, "next_cursor": cursor, "retrieved_at": now()})
            if not cursor:
                result["complete"] = True
                break
            if cursor in seen:
                raise ValueError("分页位置重复，完整性未知")
            seen.add(cursor)
        result["next_cursor"] = cursor
        result["fetched_at"] = now()
        result["requests"] = self.requests[start_index:]
        return result
