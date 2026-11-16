"""临时目录中的合成虚拟账户验收，不读取任何真实账户。"""
import copy
import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


import pm_paper.account as module
PaperAccount = module.PaperAccount


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def item(mid="1", action="BUY", budget="10", token=None):
    return {"market_id": mid, "event_id": "event", "title": "合成比赛" + mid,
            "condition_id": "condition" + mid, "token_id": token or "token" + mid,
            "outcome": "Yes", "action": action, "budget": budget, "quantity": "1",
            "limit_price": "0.6" if action == "BUY" else "0.1", "reason": "合成验收",
            "probability": None, "support": "合成支持", "counterevidence": "合成反证",
            "sources": [], "rules_text": "合成完整规则", "rules_hash": "合成目录复合摘要"}


def decision(identity="one", items=None):
    return {"decision_id": identity, "created_at": now(), "items": items or [item()], "policy_version": "合成测试第一版"}


def snapshot(items=None, fee="0", size="10000", minimum="1"):
    result = {"started_at": now(), "fetched_at": now(), "markets": {}, "books": {}, "fees": {}}
    for row in items or [item()]:
        mid, token = row["market_id"], row["token_id"]
        result["markets"][mid] = {
            "gamma": {"id": mid, "conditionId": row["condition_id"], "question": row["title"],
                      "description": "合成完整规则", "active": True, "closed": False,
                      "acceptingOrders": True, "events": [{"id": "event"}],
                      "clobTokenIds": json.dumps([token, "other" + mid]), "outcomes": '["Yes","No"]'},
            "clob": {"condition_id": row["condition_id"], "closed": False, "accepting_orders": True,
                     "tokens": [{"token_id": token, "outcome": "Yes", "winner": False, "price": "0.5"},
                                {"token_id": "other" + mid, "outcome": "No", "winner": False, "price": "0.5"}]},
        }
        result["books"][token] = {"asset_id": token, "market": row["condition_id"], "timestamp": now(), "min_order_size": minimum,
                                   "asks": [{"price": "0.5", "size": size}],
                                   "bids": [{"price": "0.4", "size": size}]}
        result["fees"][mid] = {"c": row["condition_id"], "t": [{"t": token, "o": "Yes"}, {"t": "other" + mid, "o": "No"}], "fd": {"r": fee, "e": "1"}}
    return result


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "合成账户.sqlite"
        self.account = PaperAccount(self.path, create=True)

    def tearDown(self):
        self.account.close()
        self.temp.cleanup()

    def buy(self, identity="buy", row=None, **quote_args):
        row = row or item()
        self.account.freeze(decision(identity, [row]))
        quote = snapshot([row], **quote_args)
        result = self.account.execute(identity, quote)
        self.assertEqual(result["items"][0]["status"], "filled")
        return quote, result

    def test_initialization_is_real_time_and_idempotent(self):
        first = self.account.state()
        other = PaperAccount(self.path)
        try:
            self.assertEqual(first, other.state())
            self.assertEqual(first["cash"], "1000")
            self.assertIsNotNone(dt.datetime.fromisoformat(first["started_at"]).tzinfo)
            self.assertEqual(other.db.execute("SELECT count(*) FROM journal").fetchone()[0], 1)
        finally:
            other.close()

    def test_freeze_reuse_conflict_and_append_only(self):
        value = decision()
        frozen = self.account.freeze(value)
        self.assertEqual(self.account.freeze(copy.deepcopy(value)), frozen)
        changed = copy.deepcopy(value)
        changed["items"][0]["reason"] = "不同内容"
        with self.assertRaises(ValueError):
            self.account.freeze(changed)
        with self.assertRaises(Exception):
            self.account.db.execute("UPDATE decisions SET frozen_at='2000'")

    def test_same_token_multiple_actions_rejected(self):
        with self.assertRaises(ValueError):
            self.account.freeze(decision(items=[item(), item(action="SELL")]))

    def test_freeze_after_quote_is_rejected_and_execution_is_final(self):
        quote = snapshot()
        self.account.freeze(decision())
        result = self.account.execute("one", quote)
        self.assertEqual(result["items"][0]["status"], "rejected")
        self.assertEqual(self.account.execute("one", snapshot()), result)
        self.assertEqual(self.account.state()["cash"], "1000")

    def test_buy_fee_conservation_and_execute_idempotency(self):
        quote, result = self.buy(fee="0.02")
        state = self.account.state()
        cost = Decimal(state["positions"]["token1"]["entry_cost"])
        self.assertEqual(Decimal(state["cash"]) + cost, Decimal("1000"))
        self.assertLessEqual(cost, Decimal("10"))
        self.assertGreater(Decimal(result["items"][0]["fee"]), 0)
        self.assertEqual(self.account.execute("buy", quote), result)
        self.assertEqual(self.account.state(), state)

    def test_same_snapshot_token_depth_not_consumed_twice(self):
        self.account.freeze(decision("first"))
        self.account.freeze(decision("second"))
        quote = snapshot()
        self.account.execute("first", quote)
        after = self.account.state()
        result = self.account.execute("second", quote)
        self.assertEqual(result["items"][0]["status"], "rejected")
        self.assertEqual(after, self.account.state())

    def test_missing_fee_book_minimum_and_empty_asks_never_spend(self):
        for index, missing in enumerate(("fee", "book", "minimum", "asks")):
            identity = "missing" + str(index)
            self.account.freeze(decision(identity))
            quote = snapshot()
            if missing == "fee":
                quote["fees"] = {}
            elif missing == "book":
                quote["books"] = {}
            elif missing == "minimum":
                quote["books"]["token1"].pop("min_order_size")
            else:
                quote["books"]["token1"]["asks"] = []
            result = self.account.execute(identity, quote)
            self.assertEqual(result["items"][0]["status"], "rejected")
            self.assertEqual(self.account.state()["cash"], "1000")

    def test_partial_buy_and_partial_exit_valuation(self):
        quote, result = self.buy(size="3")
        self.assertEqual(Decimal(result["items"][0]["filled"]), 3)
        quote["books"]["token1"]["bids"][0]["size"] = "1"
        report = self.account.report(quote)
        self.assertEqual(Decimal(report["unfilled_total"]), 2)
        self.assertEqual(Decimal(report["exit_value_known"]), Decimal("0.4"))
        self.assertEqual(Decimal(report["equity"]), Decimal("998.9"))
        self.assertIsNone(report["unrealized_pnl"])

    def test_sell_allocates_cost_and_conserves_cash(self):
        self.buy()
        row = item(action="SELL")
        row["quantity"] = "5"
        self.account.freeze(decision("sell", [row]))
        result = self.account.execute("sell", snapshot([row]))
        self.assertEqual(result["items"][0]["status"], "filled")
        state = self.account.state()
        self.assertEqual(Decimal(state["cash"]), Decimal("992"))
        self.assertEqual(Decimal(state["realized_pnl"]), Decimal("-0.5"))
        self.assertEqual(Decimal(state["positions"]["token1"]["entry_cost"]), Decimal("7.5"))
        self.assertEqual(Decimal(state["cash"]) + Decimal(state["positions"]["token1"]["entry_cost"]), Decimal("1000") + Decimal(state["realized_pnl"]))

    def test_buy_and_sell_identity_rule_and_asset_checks(self):
        self.buy()
        for index, change in enumerate(("condition", "outcome", "rules", "asset", "closed")):
            row = item(action="SELL")
            identity = "bad-sell" + str(index)
            self.account.freeze(decision(identity, [row]))
            quote = snapshot([row])
            if change == "condition":
                quote["markets"]["1"]["clob"]["condition_id"] = "wrong"
            elif change == "outcome":
                quote["markets"]["1"]["clob"]["tokens"][0]["outcome"] = "wrong"
            elif change == "rules":
                quote["markets"]["1"]["gamma"]["description"] = "改变规则"
            elif change == "asset":
                quote["books"]["token1"]["asset_id"] = "wrong"
            else:
                quote["markets"]["1"]["gamma"]["closed"] = True
            before = self.account.state()
            self.assertEqual(self.account.execute(identity, quote)["items"][0]["status"], "rejected")
            self.assertEqual(before, self.account.state())

    def test_unknown_valuation_is_null_and_reports_cash(self):
        quote, _ = self.buy()
        for missing in ("fees", "books"):
            copy_quote = copy.deepcopy(quote)
            copy_quote[missing] = {}
            report = self.account.report(copy_quote)
            self.assertIsNone(report["equity"])
            self.assertIsNone(report["unrealized_pnl"])
            self.assertEqual(report["unknown_valuation_markets"], ["1"])
            self.assertEqual(report["known_exit_equity"], self.account.state()["cash"])
            self.assertIsNone(report["unable_to_exit_quantity"]["token1"])
            self.assertIsNone(report["positions"][0]["unable_to_exit_quantity"])
            self.assertIsNone(report["unfilled_total"])
            self.assertEqual(Decimal(report["known_unfilled_total"]), 0)
            self.assertEqual(Decimal(report["unknown_valuation_quantity"]), 20)

    def test_formal_settlement_idempotent_and_price_alone_not_result(self):
        quote, _ = self.buy()
        current = quote["markets"]["1"]
        current["clob"]["tokens"][0].update(winner=True, price="1")
        current["clob"]["tokens"][1].update(winner=False, price="0")
        self.assertEqual(self.account.review(quote)["settled"], [])
        current["gamma"].update(closed=True, umaResolutionStatus="resolved", outcomePrices=['1', '0'])
        current["clob"]["closed"] = True
        result = self.account.review(quote)
        self.assertEqual(len(result["settled"]), 1)
        state = self.account.state()
        self.assertEqual(Decimal(state["cash"]), Decimal("1010"))
        self.assertEqual(Decimal(state["realized_pnl"]), Decimal("10"))
        self.assertEqual(self.account.review(quote)["settled"], [])
        self.assertEqual(self.account.state(), state)

    def test_refund_and_missing_formal_result_remain_pending(self):
        quote, _ = self.buy()
        quote["markets"]["1"]["gamma"].update(closed=True, umaResolutionStatus="resolved", canceled=True)
        quote["markets"]["1"]["clob"]["closed"] = True
        self.assertEqual(len(self.account.review(quote)["pending"]), 1)
        self.assertEqual(len(self.account.state()["positions"]), 1)
        empty = snapshot()
        empty["markets"] = {}
        self.assertEqual(len(self.account.review(empty)["pending"]), 1)

    def test_snapshot_timezone_staleness_and_future_are_rejected(self):
        for index, mode in enumerate(("naive", "stale", "future")):
            identity = "clock" + str(index)
            self.account.freeze(decision(identity))
            quote = snapshot()
            if mode == "naive":
                quote["started_at"] = "2026-11-12T12:00:00"
            elif mode == "stale":
                quote["started_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=301)).isoformat()
            else:
                quote["fetched_at"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=10)).isoformat()
            self.assertEqual(self.account.execute(identity, quote)["items"][0]["status"], "rejected")
            self.assertEqual(self.account.state()["cash"], "1000")

    def test_concurrent_decisions_cannot_overspend(self):
        one, two = item("1", budget="800"), item("2", budget="800")
        self.account.freeze(decision("one", [one]))
        self.account.freeze(decision("two", [two]))
        quote = snapshot([one, two])
        def execute(identity):
            account = PaperAccount(self.path)
            try:
                return account.execute(identity, quote)
            finally:
                account.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(execute, ["one", "two"]))
        state = self.account.state()
        self.assertGreaterEqual(Decimal(state["cash"]), 0)
        self.assertEqual(Decimal(state["cash"]) + sum(Decimal(pos["entry_cost"]) for pos in state["positions"].values()), Decimal("1000"))
        self.assertEqual(state["buy_count"], 2)

    def test_concurrent_same_decision_is_once(self):
        self.account.freeze(decision("same"))
        quote = snapshot()
        def execute(_):
            account = PaperAccount(self.path)
            try:
                return account.execute("same", quote)
            finally:
                account.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(execute, [1, 2]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.account.state()["buy_count"], 1)
        self.assertEqual(self.account.db.execute("SELECT count(*) FROM executions").fetchone()[0], 1)

    def test_multiple_positions_same_event_without_old_fixed_caps(self):
        rows = [item(str(i), budget="300") for i in range(1, 5)]
        self.account.freeze(decision("many", rows))
        result = self.account.execute("many", snapshot(rows))
        self.assertEqual(len([row for row in result["items"] if row["status"] == "filled"]), 4)
        state = self.account.state()
        self.assertEqual(len(state["positions"]), 4)
        self.assertEqual(state["buy_count"], 4)
        self.assertEqual(Decimal(state["cash"]), 0)

    def test_partial_sell_preserves_remaining_quantity_and_cost(self):
        self.buy()
        row = item(action="SELL")
        row["quantity"] = "10"
        self.account.freeze(decision("partial-sell", [row]))
        quote = snapshot([row], size="3")
        result = self.account.execute("partial-sell", quote)
        self.assertEqual(Decimal(result["items"][0]["filled"]), 3)
        self.assertEqual(Decimal(result["items"][0]["unfilled"]), 7)
        pos = self.account.state()["positions"]["token1"]
        self.assertEqual(Decimal(pos["quantity"]), 17)
        self.assertEqual(Decimal(pos["entry_cost"]), Decimal("8.5"))

    def test_full_sell_roundtrip_conserves_fees(self):
        self.buy(fee="0.02")
        before = self.account.state()
        row = item(action="SELL")
        row["quantity"] = before["positions"]["token1"]["quantity"]
        self.account.freeze(decision("close", [row]))
        result = self.account.execute("close", snapshot([row], fee="0.02"))
        self.assertEqual(result["items"][0]["status"], "filled")
        state = self.account.state()
        self.assertEqual(state["positions"], {})
        self.assertEqual(Decimal(state["cash"]) - Decimal("1000"), Decimal(state["realized_pnl"]))

    def test_existing_unrelated_database_is_not_reused(self):
        import sqlite3
        unrelated = Path(self.temp.name) / "旧格式合成库.sqlite"
        db = sqlite3.connect(unrelated)
        db.execute("CREATE TABLE old_account(value TEXT)")
        db.execute("INSERT INTO old_account VALUES('preserve')")
        db.commit()
        db.close()
        before = unrelated.read_bytes()
        with self.assertRaises(ValueError):
            PaperAccount(unrelated)
        self.assertEqual(unrelated.read_bytes(), before)

    def test_counterpart_token_corruption_blocks_settlement(self):
        quote, _ = self.buy()
        current = quote["markets"]["1"]
        current["gamma"].update(closed=True, umaResolutionStatus="resolved", outcomePrices=['1', '0'])
        current["clob"]["closed"] = True
        current["clob"]["tokens"][0].update(winner=True, price="1")
        current["clob"]["tokens"][1].update(outcome="Yes", winner=False, price="0")
        self.assertEqual(len(self.account.review(quote)["pending"]), 1)
        self.assertEqual(self.account.state()["settlement_count"], 0)

    def test_frozen_full_rules_are_required(self):
        for field in ("rules_text", "rules_hash"):
            value = decision("missing-" + field)
            value["items"][0].pop(field)
            with self.assertRaises(ValueError):
                self.account.freeze(value)
            value["items"][0][field] = " "
            with self.assertRaises(ValueError):
                self.account.freeze(value)

    def test_book_timestamp_and_condition_cannot_be_refreshed_by_request(self):
        for index, mode in enumerate(("stale", "future", "missing", "wrong-market")):
            identity = "book-clock-" + str(index)
            self.account.freeze(decision(identity))
            quote = snapshot()
            book = quote["books"]["token1"]
            if mode == "stale":
                book["timestamp"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=301)).isoformat()
            elif mode == "future":
                book["timestamp"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=10)).isoformat()
            elif mode == "missing":
                book.pop("timestamp")
            else:
                book["market"] = "wrong"
            result = self.account.execute(identity, quote)
            self.assertEqual(result["items"][0]["status"], "rejected")
            self.assertEqual(self.account.state()["cash"], "1000")

    def test_book_millisecond_epoch_timestamp_is_supported(self):
        self.account.freeze(decision())
        quote = snapshot()
        quote["books"]["token1"]["timestamp"] = str(int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000))
        self.assertEqual(self.account.execute("one", quote)["items"][0]["status"], "filled")

    def test_failure_after_state_change_rolls_back_state_depth_and_trade_log(self):
        self.account.freeze(decision("first"))
        self.account.freeze(decision("second"))
        quote = snapshot()
        original_log = self.account._log
        def failing_log(kind, payload):
            if kind == "模拟买入":
                raise RuntimeError("合成成交日志失败")
            return original_log(kind, payload)
        self.account._log = failing_log
        first = self.account.execute("first", quote)
        self.assertEqual(first["items"][0]["status"], "rejected")
        self.assertNotIn("filled", first["items"][0])
        self.assertEqual(self.account.state()["cash"], "1000")
        self.assertEqual(self.account.state()["positions"], {})
        self.assertEqual(self.account.db.execute("SELECT count(*) FROM consumed_book_depth").fetchone()[0], 0)
        self.assertEqual(self.account.db.execute("SELECT count(*) FROM journal WHERE kind='模拟买入'").fetchone()[0], 0)
        self.account._log = original_log
        self.assertEqual(self.account.execute("second", quote)["items"][0]["status"], "filled")

    def test_later_item_failure_preserves_only_earlier_committed_item(self):
        rows = [item("1"), item("2")]
        self.account.freeze(decision("two", rows))
        original_log = self.account._log
        def failing_second(kind, payload):
            if kind == "模拟买入" and payload["market_id"] == "2":
                original_log(kind, payload)
                raise RuntimeError("合成第二项日志后失败")
            return original_log(kind, payload)
        self.account._log = failing_second
        result = self.account.execute("two", snapshot(rows))
        self.assertEqual([row["status"] for row in result["items"]], ["filled", "rejected"])
        self.assertEqual(set(self.account.state()["positions"]), {"token1"})
        self.assertEqual(self.account.state()["buy_count"], 1)
        self.assertEqual(self.account.db.execute("SELECT count(*) FROM journal WHERE kind='模拟买入'").fetchone()[0], 1)
        self.assertEqual(self.account.db.execute("SELECT count(*) FROM consumed_book_depth").fetchone()[0], 1)

    def test_execution_lookup_is_read_only(self):
        self.assertIsNone(self.account.execution("absent"))
        quote, result = self.buy()
        before = self.account.state()
        count = self.account.db.execute("SELECT count(*) FROM journal").fetchone()[0]
        self.assertEqual(self.account.execution("buy"), result)
        self.assertEqual(self.account.state(), before)
        self.assertEqual(self.account.db.execute("SELECT count(*) FROM journal").fetchone()[0], count)

    def test_fee_wrong_condition_cannot_spend(self):
        self.account.freeze(decision())
        quote = snapshot()
        quote["fees"]["1"]["c"] = "other-condition"
        before = self.account.state()
        result = self.account.execute("one", quote)
        self.assertEqual(result["items"][0]["status"], "rejected")
        self.assertIn("费用原件", result["items"][0]["reason"])
        self.assertEqual(self.account.state(), before)

    def test_fee_wrong_token_cannot_spend(self):
        self.account.freeze(decision())
        quote = snapshot()
        quote["fees"]["1"]["t"][1]["t"] = "foreign-token"
        before = self.account.state()
        result = self.account.execute("one", quote)
        self.assertEqual(result["items"][0]["status"], "rejected")
        self.assertIn("费用原件", result["items"][0]["reason"])
        self.assertEqual(self.account.state(), before)

    def test_missing_fee_identity_or_incomplete_mapping_cannot_spend(self):
        for index, mode in enumerate(("c", "t", "incomplete", "duplicate", "outcome")):
            identity = "fee-identity-" + str(index)
            self.account.freeze(decision(identity))
            quote = snapshot()
            fee = quote["fees"]["1"]
            if mode in ("c", "t"):
                fee.pop(mode)
            elif mode == "incomplete":
                fee["t"].pop()
            elif mode == "duplicate":
                fee["t"][1] = dict(fee["t"][0])
            else:
                fee["t"][0]["o"] = "unknown"
            self.assertEqual(self.account.execute(identity, quote)["items"][0]["status"], "rejected")
            self.assertEqual(self.account.state()["cash"], "1000")

    def test_sell_and_valuation_require_bound_fee_identity(self):
        self.buy()
        row = item(action="SELL")
        self.account.freeze(decision("sell-wrong-fee", [row]))
        quote = snapshot([row])
        quote["fees"]["1"]["t"][0]["t"] = "foreign-token"
        before = self.account.state()
        result = self.account.execute("sell-wrong-fee", quote)
        self.assertEqual(result["items"][0]["status"], "rejected")
        self.assertEqual(self.account.state(), before)
        report = self.account.report(quote)
        self.assertIsNone(report["equity"])
        self.assertEqual(report["unknown_valuation_markets"], ["1"])

    def test_local_receipt_times_cannot_reuse_same_source_depth(self):
        self.account.freeze(decision("first"))
        self.account.freeze(decision("second"))
        quote = snapshot(size="1")
        first = self.account.execute("first", quote)
        self.assertEqual(Decimal(first["items"][0]["filled"]), 1)
        after = self.account.state()
        reread = copy.deepcopy(quote)
        reread["started_at"], reread["fetched_at"] = now(), now()
        second = self.account.execute("second", reread)
        self.assertEqual(second["items"][0]["status"], "rejected")
        self.assertEqual(self.account.state(), after)
        self.assertEqual(Decimal(self.account.state()["positions"]["token1"]["quantity"]), 1)

    def test_source_depth_identity_ignores_level_order_and_decimal_format(self):
        self.account.freeze(decision("first"))
        self.account.freeze(decision("second"))
        quote = snapshot(size="1")
        quote["books"]["token1"]["asks"].append({"price": "0.55", "size": "1"})
        self.account.execute("first", quote)
        reread = copy.deepcopy(quote)
        reread["started_at"], reread["fetched_at"] = now(), now()
        reread["books"]["token1"]["asks"] = [{"price": "0.550", "size": "1.0"}, {"price": "0.50", "size": "1.00"}]
        self.assertEqual(self.account.execute("second", reread)["items"][0]["status"], "rejected")
        self.assertEqual(Decimal(self.account.state()["positions"]["token1"]["quantity"]), 2)

    def test_opposite_sides_have_independent_source_depth(self):
        self.account.freeze(decision("buy"))
        sell = item(action="SELL")
        self.account.freeze(decision("sell", [sell]))
        quote = snapshot()
        self.account.execute("buy", quote)
        self.assertEqual(self.account.execute("sell", quote)["items"][0]["status"], "filled")
        self.assertEqual(self.account.db.execute("SELECT count(*) FROM consumed_book_depth").fetchone()[0], 2)

    def test_new_source_timestamp_can_provide_new_depth(self):
        self.account.freeze(decision("first"))
        self.account.freeze(decision("second"))
        quote = snapshot(size="1")
        self.account.execute("first", quote)
        updated = snapshot(size="1")
        self.assertNotEqual(updated["books"]["token1"]["timestamp"], quote["books"]["token1"]["timestamp"])
        self.assertEqual(self.account.execute("second", updated)["items"][0]["status"], "filled")
        self.assertEqual(Decimal(self.account.state()["positions"]["token1"]["quantity"]), 2)

    def test_legacy_execution_depth_is_preserved_and_blocks_reread(self):
        self.account.freeze(decision("legacy"))
        self.account.freeze(decision("next"))
        quote = snapshot(size="1")
        legacy = {"decision_id": "legacy", "executed_at": now(), "snapshot_hash": module._hash(quote), "snapshot_at": quote["fetched_at"], "items": [{"market_id": "1", "token_id": "token1", "action": "BUY", "status": "filled", "filled": "1", "cost": "0.5", "fee": "0", "book_timestamp": quote["books"]["token1"]["timestamp"], "book_market": "condition1"}]}
        original_payload = json.dumps(legacy, ensure_ascii=False)
        self.account.db.execute("INSERT INTO executions VALUES(?,?,?)", ("legacy", now(), original_payload))
        self.account.db.execute("INSERT INTO consumed_quotes VALUES(?,?,?)", (module._hash(quote), "token1", "legacy"))
        state = self.account.state()
        state["cash"] = "999.5"
        state["buy_count"] = 1
        state["positions"]["token1"] = dict(item(), quantity="1", entry_cost="0.5", opened_at=now())
        self.account._save(state)
        self.account.db.execute("PRAGMA user_version=1")
        self.account.close()
        self.account = PaperAccount(self.path, create=True)
        self.assertEqual(self.account.state(), state)
        self.assertEqual(self.account.db.execute("SELECT payload FROM executions WHERE decision_id='legacy'").fetchone()[0], original_payload)
        self.assertEqual(self.account.db.execute("SELECT count(*) FROM consumed_quotes").fetchone()[0], 1)
        self.assertEqual(self.account.db.execute("SELECT count(*) FROM legacy_book_depth").fetchone()[0], 1)
        reread = copy.deepcopy(quote)
        reread["started_at"], reread["fetched_at"] = now(), now()
        self.assertEqual(self.account.execute("next", reread)["items"][0]["status"], "rejected")
        self.assertEqual(self.account.state(), state)
        self.assertEqual(self.account.db.execute("SELECT payload FROM executions WHERE decision_id='legacy'").fetchone()[0], original_payload)

    def test_stale_source_book_quantity_is_unknown_not_confirmed_unfilled(self):
        quote, _ = self.buy(size="3")
        quote["books"]["token1"]["timestamp"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=301)).isoformat()
        report = self.account.report(quote)
        self.assertIsNone(report["equity"])
        self.assertIsNone(report["unfilled_total"])
        self.assertEqual(report["known_unfilled_total"], "0")
        self.assertEqual(Decimal(report["unknown_valuation_quantity"]), 3)
        self.assertIsNone(report["unable_to_exit_quantity"]["token1"])
        self.assertIsNone(report["total_pnl"])
        self.assertIsNone(report["unrealized_pnl"])

    def test_known_depth_shortage_and_unknown_valuation_quantities_are_separate(self):
        rows = [item("1"), item("2")]
        self.account.freeze(decision("two-valuations", rows))
        quote = snapshot(rows, size="3")
        self.account.execute("two-valuations", quote)
        quote["books"]["token1"]["bids"][0]["size"] = "1"
        quote["fees"].pop("2")
        before = self.account.state()
        report = self.account.report(quote)
        self.assertEqual(Decimal(report["known_unfilled_total"]), 2)
        self.assertEqual(Decimal(report["unknown_valuation_quantity"]), 3)
        self.assertIsNone(report["unfilled_total"])
        self.assertEqual(Decimal(report["unable_to_exit_quantity"]["token1"]), 2)
        self.assertIsNone(report["unable_to_exit_quantity"]["token2"])
        self.assertEqual(Decimal(report["exit_value_known"]), Decimal("0.4"))
        self.assertIsNone(report["equity"])
        self.assertIsNone(report["unrealized_pnl"])
        self.assertIsNone(report["total_pnl"])
        self.assertEqual(self.account.state(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
