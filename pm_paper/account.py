"""从现有体育自主模拟执行账本提取的通用执行与记账模块。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path


from .fills import buy_value, sell_value, fee_rate, final_winner
ZERO = Decimal("0")
APPLICATION_ID = 1397772114
QUOTE_MAX_AGE_SECONDS = 300


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _time(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return parsed


def _decimal(value):
    if isinstance(value, bool):
        raise ValueError("布尔值不能作为金额或数量")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("金额或数量必须有限")
    return result


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _list(value):
    return json.loads(value) if isinstance(value, str) else value


def _snapshot_times(snapshot, current, fresh=True):
    started, fetched = _time(snapshot["started_at"]), _time(snapshot["fetched_at"])
    if not started <= fetched <= current:
        raise ValueError("报价开始、取得及执行时间顺序不符")
    if fresh and (current - started).total_seconds() > QUOTE_MAX_AGE_SECONDS:
        raise ValueError("报价超过本账户选择的300秒有效期")
    return started, fetched


def _book_time(value):
    if isinstance(value, (int, float, Decimal)) or isinstance(value, str) and value.replace(".", "", 1).isdigit():
        number = _decimal(value)
        seconds = number / 1000 if number > Decimal("100000000000") else number
        try:
            return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(microseconds=int(seconds * 1000000))
        except (OverflowError, ValueError) as exc:
            raise ValueError("盘口自身时间不合法") from exc
    return _time(value)


def _depth_identity(book, action):
    side = "asks" if action == "BUY" else "bids"
    book_at = _book_time(book["timestamp"]).astimezone(dt.timezone.utc).isoformat()
    levels = sorted((_decimal(row["price"]), _decimal(row["size"])) for row in book[side])
    content = {
        "market": book["market"], "asset_id": str(book["asset_id"]), "side": side,
        "book_at": book_at,
        "levels": [[str(price.normalize()), str(size.normalize())] for price, size in levels],
    }
    return _hash(content), side, book_at


def _identity(item, snapshot, trading=True):
    mid, token = str(item["market_id"]), str(item["token_id"])
    market = snapshot.get("markets", {}).get(mid)
    if not market:
        raise ValueError("缺当前市场原件")
    gamma, clob = market["gamma"], market["clob"]
    if str(gamma.get("id")) != mid or gamma.get("conditionId") != item["condition_id"]:
        raise ValueError("市场或条件标识与冻结依据不符")
    if clob.get("condition_id") != item["condition_id"]:
        raise ValueError("订单簿市场的条件标识不符")
    if not str(gamma.get("description", "")).strip():
        raise ValueError("缺完整市场规则正文")
    if item.get("rules_text") is not None and gamma["description"] != item["rules_text"]:
        raise ValueError("当前完整规则与冻结决策正文不一致")
    if gamma.get("question") and gamma["question"] != item["title"]:
        raise ValueError("市场标题与冻结依据不符")
    events = gamma.get("events")
    if events is not None and str(item["event_id"]) not in {str(event.get("id")) for event in events}:
        raise ValueError("事件归属与冻结依据不符")
    tokens = clob.get("tokens") or []
    matches = [row for row in tokens if str(row.get("token_id")) == token and row.get("outcome") == item["outcome"]]
    if len(matches) != 1:
        raise ValueError("结果与订单簿代币对应不唯一")
    ids, outcomes = _list(gamma.get("clobTokenIds")), _list(gamma.get("outcomes"))
    if not isinstance(ids, list) or not isinstance(outcomes, list) or len(ids) != len(outcomes):
        raise ValueError("公开市场代币和结果表不完整")
    gamma_pairs = [(str(t), outcome) for t, outcome in zip(ids, outcomes)]
    clob_pairs = [(str(row.get("token_id")), row.get("outcome")) for row in tokens]
    if len(set(map(str, ids))) != len(ids) or len(set(outcomes)) != len(outcomes) or len(clob_pairs) != len(gamma_pairs) or set(clob_pairs) != set(gamma_pairs):
        raise ValueError("两份公开原件的完整代币与结果表不一致")
    if [(str(t), o) for t, o in zip(ids, outcomes)].count((token, item["outcome"])) != 1:
        raise ValueError("公开市场代币与结果对应不符")
    if trading and not (gamma.get("active") is True and gamma.get("closed") is False and gamma.get("acceptingOrders") is True and clob.get("closed") is False and clob.get("accepting_orders") is True):
        raise ValueError("市场当前交易状态未知或不接受交易")
    return market


def _book_and_rate(item, snapshot, require_minimum):
    market = _identity(item, snapshot)
    book = snapshot.get("books", {}).get(str(item["token_id"]))
    if not isinstance(book, dict) or str(book.get("asset_id")) != str(item["token_id"]):
        raise ValueError("缺订单簿或订单簿资产标识不符")
    if book.get("market") != item["condition_id"]:
        raise ValueError("订单簿自身的市场条件标识不符或未知")
    if book.get("timestamp") is None:
        raise ValueError("盘口自身时间未知")
    book_at = _book_time(book["timestamp"])
    age = (_now() - book_at).total_seconds()
    if not 0 <= age <= QUOTE_MAX_AGE_SECONDS:
        raise ValueError("盘口自身时间在未来或超过300秒有效期")
    if not isinstance(book.get("bids"), list) or not isinstance(book.get("asks"), list):
        raise ValueError("订单簿买卖深度未知")
    for side in ("bids", "asks"):
        for level in book[side]:
            if not 0 < _decimal(level["price"]) < 1 or _decimal(level["size"]) < 0:
                raise ValueError("订单簿价格或数量不合法")
    fee = snapshot.get("fees", {}).get(str(item["market_id"]))
    if not isinstance(fee, dict) or fee.get("c") != item["condition_id"]:
        raise ValueError("费用原件的市场条件标识不符或未知")
    fee_tokens = fee.get("t")
    if not isinstance(fee_tokens, list) or any(not isinstance(row, dict) for row in fee_tokens):
        raise ValueError("费用原件的完整代币结果表未知")
    fee_pairs = [(str(row.get("t")), row.get("o")) for row in fee_tokens]
    market_pairs = [(str(row.get("token_id")), row.get("outcome")) for row in market["clob"]["tokens"]]
    if len(fee_pairs) != len(market_pairs) or set(fee_pairs) != set(market_pairs):
        raise ValueError("费用原件的完整代币结果表与市场原件不一致")
    rate = fee_rate(snapshot, str(item["market_id"]))
    if rate is None:
        raise ValueError("费用未知或费用公式不受当前纯函数支持")
    minimum = None
    if require_minimum:
        if book.get("min_order_size") is None:
            raise ValueError("订单簿最小数量未知")
        minimum = _decimal(book["min_order_size"])
        if minimum <= 0:
            raise ValueError("订单簿最小数量不合法")
    return book, rate, minimum


def validate_decision(decision):
    if not isinstance(decision.get("decision_id"), str) or not decision["decision_id"] or not decision.get("policy_version"):
        raise ValueError("缺决策标识或实际政策版本")
    if _time(decision["created_at"]) > _now():
        raise ValueError("决策创建时间在未来")
    if not isinstance(decision.get("items"), list):
        raise ValueError("决策项目必须为列表")
    seen = set()
    for item in decision["items"]:
        for field in ("market_id", "event_id", "title", "condition_id", "token_id", "outcome", "reason", "probability", "support", "counterevidence", "sources", "rules_text", "rules_hash"):
            if field not in item:
                raise ValueError("决策缺字段" + field)
        if any(not isinstance(item[field], str) or not item[field].strip() for field in ("rules_text", "rules_hash")):
            raise ValueError("冻结规则正文和原目录规则摘要必须非空")
        token = str(item["token_id"])
        if token in seen:
            raise ValueError("同一决策不接受同一代币的重复动作")
        seen.add(token)
        if item["action"] not in {"BUY", "SELL", "WATCH", "HOLD"}:
            raise ValueError("决策动作不合法")
        if item["probability"] is not None and not ZERO <= _decimal(item["probability"]) <= 1:
            raise ValueError("概率必须为空或零到一的有限数")
        if item["action"] in {"BUY", "SELL"}:
            amount = item["budget"] if item["action"] == "BUY" else item["quantity"]
            if not isinstance(amount, str) or _decimal(amount) <= 0:
                raise ValueError("预算或卖出份额必须为正数字符串")
            if not isinstance(item.get("limit_price"), str) or not 0 < _decimal(item["limit_price"]) < 1:
                raise ValueError("限价必须为零到一之间的数字字符串")


class PaperAccount:
    """持久化模拟账户，保存冻结决定、一次执行、逐笔资金与持仓。"""

    def __init__(self, path, *, create=False, initial_cash="1000"):
        self.path = Path(path)
        if not self.path.exists() and not create:
            raise FileNotFoundError("账户文件不存在，请明确创建或指定已有账户")
        capital = _decimal(initial_cash)
        if capital <= 0:
            raise ValueError("初始虚拟本金必须为正数")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.execute("PRAGMA busy_timeout=30000")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            application = self.db.execute("PRAGMA application_id").fetchone()[0]
            tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if application != APPLICATION_ID and (application or tables):
                raise ValueError("数据库已有其他账户或数据，请使用兼容的独立模拟账户文件")
            self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            for sql in [
                "CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY CHECK(id=1),state TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS decisions(decision_id TEXT PRIMARY KEY,hash TEXT NOT NULL,frozen_at TEXT NOT NULL,payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS executions(decision_id TEXT PRIMARY KEY,at TEXT NOT NULL,payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS journal(seq INTEGER PRIMARY KEY,at TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS consumed_quotes(snapshot_hash TEXT NOT NULL,token_id TEXT NOT NULL,decision_id TEXT NOT NULL,PRIMARY KEY(snapshot_hash,token_id))",
                "CREATE TABLE IF NOT EXISTS consumed_book_depth(depth_id TEXT NOT NULL,token_id TEXT NOT NULL,side TEXT NOT NULL,book_at TEXT NOT NULL,decision_id TEXT NOT NULL,PRIMARY KEY(depth_id,token_id,side))",
                "CREATE TABLE IF NOT EXISTS legacy_book_depth(token_id TEXT NOT NULL,side TEXT NOT NULL,book_at TEXT NOT NULL,decision_id TEXT NOT NULL,PRIMARY KEY(token_id,side,book_at))",
            ]:
                self.db.execute(sql)
            for table in ("decisions", "executions", "journal", "consumed_quotes", "consumed_book_depth", "legacy_book_depth"):
                for operation in ("UPDATE", "DELETE"):
                    self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'历史记录不可改写'); END")
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 2:
                # 旧执行缺少源端价量摘要，按原成交的代币、方向、源端时刻追加占用依据。
                for decision_id, payload in self.db.execute("SELECT decision_id,payload FROM executions").fetchall():
                    for item in json.loads(payload).get("items", []):
                        if item.get("status") != "filled" or item.get("book_depth_id") or item.get("action") not in {"BUY", "SELL"}:
                            continue
                        book_at = _book_time(item["book_timestamp"]).astimezone(dt.timezone.utc).isoformat()
                        side = "asks" if item["action"] == "BUY" else "bids"
                        self.db.execute("INSERT OR IGNORE INTO legacy_book_depth VALUES(?,?,?,?)", (str(item["token_id"]), side, book_at, decision_id))
                self.db.execute("PRAGMA user_version=2")
            if not self.db.execute("SELECT 1 FROM account WHERE id=1").fetchone():
                initial = {"initial_cash": str(capital), "cash": str(capital), "realized_pnl": "0", "positions": {}, "started_at": _now().isoformat(), "buy_count": 0, "sell_count": 0, "settlement_count": 0}
                self.db.execute("INSERT INTO account VALUES(1,?)", (_json(initial),))
                self._log("初始化虚拟本金", initial)
            self.db.execute("COMMIT")
        except Exception:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def state(self):
        return json.loads(self.db.execute("SELECT state FROM account WHERE id=1").fetchone()[0])

    def execution(self, decision_id):
        row = self.db.execute("SELECT payload FROM executions WHERE decision_id=?", (decision_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, state):
        if _decimal(state["cash"]) < 0 or any(_decimal(pos["quantity"]) <= 0 or _decimal(pos["entry_cost"]) < 0 for pos in state["positions"].values()):
            raise ValueError("账户资金或持仓数量不合法")
        self.db.execute("UPDATE account SET state=? WHERE id=1", (_json(state),))

    def _log(self, kind, payload):
        self.db.execute("INSERT INTO journal(at,kind,payload) VALUES(?,?,?)", (_now().isoformat(), kind, _json(payload)))

    def freeze(self, decision):
        validate_decision(decision)
        identity, content_hash = decision["decision_id"], _hash(decision)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute("SELECT hash,frozen_at FROM decisions WHERE decision_id=?", (identity,)).fetchone()
            if existing and existing[0] != content_hash:
                raise ValueError("同一决策标识的内容冲突")
            frozen = existing[1] if existing else _now().isoformat()
            if not existing:
                self.db.execute("INSERT INTO decisions VALUES(?,?,?,?)", (identity, content_hash, frozen, _json(decision)))
                self._log("冻结真实决策", {"decision_id": identity, "hash": content_hash, "frozen_at": frozen})
            self.db.execute("COMMIT")
            return {"decision_id": identity, "hash": content_hash, "frozen_at": frozen}
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def execute(self, decision_id, snapshot):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            done = self.db.execute("SELECT payload FROM executions WHERE decision_id=?", (decision_id,)).fetchone()
            if done:
                self.db.execute("COMMIT")
                return json.loads(done[0])
            frozen = self.db.execute("SELECT frozen_at,payload FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
            if not frozen:
                raise ValueError("决策尚未在账户真实冻结")
            decision, current = json.loads(frozen[1]), _now()
            result = {"decision_id": decision_id, "executed_at": current.isoformat(), "snapshot_hash": _hash(snapshot), "snapshot_at": snapshot.get("fetched_at"), "items": []}
            state = self.state()
            quote_error = None
            try:
                started, _ = _snapshot_times(snapshot, current)
                if _time(frozen[0]) >= started:
                    raise ValueError("账户实际冻结必须早于报价请求开始")
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                quote_error = str(exc)
            for item in decision["items"]:
                record = {"market_id": str(item["market_id"]), "token_id": str(item["token_id"]), "action": item["action"]}
                before = _json(state)
                self.db.execute("SAVEPOINT item_operation")
                try:
                    if quote_error:
                        raise ValueError(quote_error)
                    if item["action"] in {"WATCH", "HOLD"}:
                        record["status"] = "recorded"
                    else:
                        self._trade(item, snapshot, state, decision_id, record)
                except Exception as exc:
                    self.db.execute("ROLLBACK TO SAVEPOINT item_operation")
                    state = json.loads(before)
                    record = {"market_id": str(item["market_id"]), "token_id": str(item["token_id"]), "action": item["action"], "status": "rejected", "reason": str(exc)}
                finally:
                    self.db.execute("RELEASE SAVEPOINT item_operation")
                result["items"].append(record)
            self._save(state)
            self.db.execute("INSERT INTO executions VALUES(?,?,?)", (decision_id, current.isoformat(), _json(result)))
            self._log("一次性模拟执行结果", result)
            self.db.execute("COMMIT")
            return result
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _trade(self, item, snapshot, state, decision_id, record):
        book, rate, minimum = _book_and_rate(item, snapshot, require_minimum=True)
        record.update(book_timestamp=book["timestamp"], book_market=book["market"])
        token = str(item["token_id"])
        depth_id, side, book_at = _depth_identity(book, item["action"])
        if self.db.execute("SELECT 1 FROM consumed_book_depth WHERE depth_id=? AND token_id=? AND side=?", (depth_id, token, side)).fetchone():
            raise ValueError("该源端盘口的同一代币同方向深度已被本账户消费")
        if self.db.execute("SELECT 1 FROM legacy_book_depth WHERE token_id=? AND side=? AND book_at=?", (token, side, book_at)).fetchone():
            raise ValueError("旧成交已消费该代币同方向同源端时刻的盘口深度")
        record.update(book_depth_id=depth_id, book_side=side)
        position = state["positions"].get(token)
        if position and any(str(position[key]) != str(item[key]) for key in ("market_id", "event_id", "condition_id", "outcome")):
            raise ValueError("已有持仓身份与本次动作不符")
        if item["action"] == "BUY":
            budget = min(_decimal(item["budget"]), _decimal(state["cash"]))
            if budget <= 0:
                raise ValueError("可用现金不足")
            fill = buy_value(book, budget, rate, _decimal(item["limit_price"]), Decimal(1))
            if fill["filled"] < minimum:
                raise ValueError("可成交数量未达到原订单簿最小数量")
            if not ZERO < fill["cost"] <= budget:
                raise ValueError("成交成本不合法")
            position = dict(position) if position else {key: item[key] for key in ("market_id", "event_id", "title", "condition_id", "token_id", "outcome")}
            if "rules_text" in item:
                position["rules_text"] = item["rules_text"]
            if "rules_hash" in item:
                position["rules_hash"] = item["rules_hash"]
            position["quantity"] = str(_decimal(position.get("quantity", "0")) + fill["filled"])
            position["entry_cost"] = str(_decimal(position.get("entry_cost", "0")) + fill["cost"])
            position["opened_at"] = position.get("opened_at", _now().isoformat())
            state["positions"][token] = position
            state["cash"] = str(_decimal(state["cash"]) - fill["cost"])
            state["buy_count"] += 1
            record.update(status="filled", filled=str(fill["filled"]), cost=str(fill["cost"]), fee=str(fill["fee"]), unused_budget=str(_decimal(item["budget"]) - fill["cost"]), fills=fill["fills"])
        else:
            if not position:
                raise ValueError("没有可卖出的持仓")
            quantity = _decimal(item["quantity"])
            owned = _decimal(position["quantity"])
            if quantity > owned:
                raise ValueError("卖出份额超过持仓")
            fill = sell_value(book, quantity, rate, Decimal(1), _decimal(item["limit_price"]))
            if fill["filled"] < minimum:
                raise ValueError("可成交数量未达到原订单簿最小数量")
            entry = _decimal(position["entry_cost"])
            used_cost = entry if fill["filled"] == owned else entry * fill["filled"] / owned
            profit = fill["net"] - used_cost
            state["cash"] = str(_decimal(state["cash"]) + fill["net"])
            state["realized_pnl"] = str(_decimal(state["realized_pnl"]) + profit)
            if fill["filled"] == owned:
                del state["positions"][token]
            else:
                state["positions"][token] = dict(position, quantity=str(owned - fill["filled"]), entry_cost=str(entry - used_cost))
            state["sell_count"] += 1
            record.update(status="filled", filled=str(fill["filled"]), unfilled=str(fill["unfilled"]), net=str(fill["net"]), fee=str(fill["fee"]), realized_pnl=str(profit), fills=fill["fills"])
        self.db.execute("INSERT INTO consumed_book_depth VALUES(?,?,?,?,?)", (depth_id, token, side, book_at, decision_id))
        self._log("模拟买入" if item["action"] == "BUY" else "模拟卖出", dict(record, decision_id=decision_id, snapshot_at=snapshot["fetched_at"]))

    def review(self, snapshot):
        _snapshot_times(snapshot, _now(), fresh=False)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            state, settled, pending = self.state(), [], []
            for token, pos in list(state["positions"].items()):
                try:
                    market = _identity(pos, snapshot, trading=False)
                    gamma, clob = market["gamma"], market["clob"]
                    if any(gamma.get(key) is True or clob.get(key) is True for key in ("cancelled", "canceled", "refunded", "void")):
                        raise ValueError("取消退款或均分路径等待明确规则和证据核验")
                    tokens = clob.get("tokens") or []
                    if gamma.get("closed") is not True or clob.get("closed") is not True or any(row.get("winner") is not True and row.get("winner") is not False for row in tokens):
                        raise ValueError("缺明确正式结束和胜方标志")
                    winner = final_winner(market)
                    if winner is None:
                        raise ValueError("正式二元结果尚未明确或存在退款均分")
                    payout = _decimal(pos["quantity"]) if pos["outcome"] == winner else ZERO
                    profit = payout - _decimal(pos["entry_cost"])
                    state["cash"] = str(_decimal(state["cash"]) + payout)
                    state["realized_pnl"] = str(_decimal(state["realized_pnl"]) + profit)
                    state["settlement_count"] += 1
                    del state["positions"][token]
                    entry = {"token_id": token, "market_id": pos["market_id"], "position": pos, "winner": winner, "payout": str(payout), "realized_pnl": str(profit), "snapshot_at": snapshot["fetched_at"], "snapshot_hash": _hash(snapshot)}
                    settled.append(entry)
                    self._log("正式结果模拟结算", entry)
                except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
                    pending.append({"token_id": token, "market_id": pos["market_id"], "reason": str(exc)})
            self._save(state)
            result = {"reviewed_at": _now().isoformat(), "snapshot_at": snapshot["fetched_at"], "settled": settled, "pending": pending}
            self._log("持仓正式结果复核", result)
            self.db.execute("COMMIT")
            return result
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def report(self, snapshot):
        state = self.state()
        known, unknown, unable, valuations = ZERO, {}, {}, []
        unknown_quantity = ZERO
        time_error = None
        try:
            _snapshot_times(snapshot, _now())
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            time_error = str(exc)
        for token, pos in state["positions"].items():
            quantity = _decimal(pos["quantity"])
            row = dict(pos)
            try:
                if time_error:
                    raise ValueError(time_error)
                book, rate, _ = _book_and_rate(pos, snapshot, require_minimum=True)
                fill = sell_value(book, quantity, rate, Decimal(1))
                if ZERO < fill["filled"] < _decimal(book["min_order_size"]):
                    fill = {"net": ZERO, "filled": ZERO, "unfilled": quantity}
                known += fill["net"]
                unable[token] = str(fill["unfilled"])
                row.update(exit_net=str(fill["net"]), liquidation_value=str(fill["net"]), executable_quantity=str(fill["filled"]), unable_to_exit_quantity=str(fill["unfilled"]), unfilled=str(fill["unfilled"]), unknown_valuation_quantity="0")
            except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
                unknown[str(pos["market_id"])] = str(exc)
                unable[token] = None
                unknown_quantity += quantity
                row.update(exit_net=None, liquidation_value=None, executable_quantity=None, unable_to_exit_quantity=None, unfilled=None, unknown_valuation_quantity=str(quantity), reason=str(exc))
            valuations.append(row)
        exit_equity = _decimal(state["cash"]) + known
        known_unfilled = sum((_decimal(qty) for qty in unable.values() if qty is not None), ZERO)
        residual = known_unfilled > 0
        open_cost = sum((_decimal(pos["entry_cost"]) for pos in state["positions"].values()), ZERO)
        return {
            "initial_cash": state["initial_cash"], "started_at": state["started_at"], "cash": state["cash"],
            "buy_count": state["buy_count"], "sell_count": state["sell_count"], "settlement_count": state["settlement_count"],
            "realized_pnl": state["realized_pnl"], "unrealized_pnl": None if unknown or residual else str(known - open_cost),
            "equity": None if unknown else str(exit_equity), "known_exit_equity": str(exit_equity),
            "known_exit_value": str(known), "unable_to_exit_quantity": unable,
            "open_entry_cost": str(open_cost), "exit_value_known": str(known),
            "total_pnl": None if unknown or residual else str(exit_equity - _decimal(state["initial_cash"])),
            "unfilled_total": None if unknown else str(known_unfilled),
            "known_unfilled_total": str(known_unfilled), "unknown_valuation_quantity": str(unknown_quantity),
            "unknown_valuation_markets": sorted(unknown), "valuation_errors": unknown, "positions": valuations,
            "quote_started_at": snapshot.get("started_at"), "quote_fetched_at": snapshot.get("fetched_at"),
            "at": snapshot.get("fetched_at"),
            "reported_at": _now().isoformat(), "quote_max_age_seconds": QUOTE_MAX_AGE_SECONDS,
        }
