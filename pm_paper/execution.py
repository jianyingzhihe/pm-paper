"""无账本依赖的执行预检，供调用方接入自己的记账程序。"""
from decimal import Decimal, ROUND_CEILING

from .account import _book_and_rate, _decimal, _now, _snapshot_times
from .fills import buy_value, sell_value, levels, CENT


def preflight(item, snapshot, cash=None):
    result = {"market_id": str(item.get("market_id")), "outcome": item.get("outcome"), "status": "blocked", "reasons": [],
              "quote_started_at": snapshot.get('started_at'), "quote_fetched_at": snapshot.get('fetched_at')}
    try:
        _snapshot_times(snapshot, _now())
        book, rate, minimum = _book_and_rate(item, snapshot, require_minimum=True)
        result['book_timestamp'] = book['timestamp']
        limit = _decimal(item["limit_price"])
        if not 0 < limit < 1:
            raise ValueError("限价需要在零到一之间")
        if item["action"] == "SELL":
            quantity = _decimal(item["quantity"])
            if quantity <= 0:
                raise ValueError("卖出份额必须为正")
            sold = sell_value(book, quantity, rate, Decimal(1), limit)
            result.update(status="feasible" if sold["filled"] >= minimum else "blocked",
                          filled=str(sold["filled"]), net=str(sold["net"]), unfilled=str(sold["unfilled"]),
                          fee=str(sold["fee"]), fills=sold["fills"], position_check="由所属账本核对持仓")
            return result
        if item["action"] != "BUY":
            raise ValueError("执行预检需要买入或卖出动作")
        budget = _decimal(item["budget"])
        if budget <= 0 or cash is not None and _decimal(cash) < 0:
            raise ValueError("金额或现金不合法")
        available = min(budget, _decimal(cash)) if cash is not None else budget
        bought = buy_value(book, available, rate, limit, Decimal(1))
        quantity = bought["filled"]
        sold = sell_value(book, quantity, rate)
        cost = bought["cost"]
        minimum_left = (minimum / CENT).to_integral_value(rounding=ROUND_CEILING) * CENT
        minimum_cost = Decimal(0)
        for price, size in levels(book, "asks"):
            if price > limit:
                break
            take = min(minimum_left, size)
            minimum_cost += take * (price + rate * price * (1 - price))
            minimum_left -= take
        if minimum_left:
            minimum_cost = None
        feasible = quantity >= minimum
        loss = (cost - sold["net"]) / cost if cost else None
        full_exit = quantity > 0 and sold["unfilled"] == 0
        exit_grade = "pending"
        if feasible:
            exit_grade = "priority" if full_exit and loss <= Decimal("0.05") else "reserve" if full_exit and loss <= Decimal("0.10") else "defer_short_term"
        result.update(status="feasible" if feasible else "blocked", budget=str(budget), cash_check=cash is not None,
                      filled=str(quantity), cost=str(cost), fee=str(bought["fee"]), fills=bought["fills"],
                      minimum_quantity=str(minimum), minimum_cost=str(minimum_cost) if minimum_cost is not None else None,
                      unit_cost=str(cost / quantity) if quantity else None, exit_net=str(sold["net"]),
                      exit_fee=str(sold["fee"]), unable_to_exit=str(sold["unfilled"]),
                      immediate_exit_loss=str(loss) if loss is not None else None, short_term_grade=exit_grade,
                      hold_to_resolution={"max_cost_loss": str(cost), "winning_payout": str(quantity),
                                          "checks": ["完整结算规则", "指定来源", "资金占用", "相关持仓及全损风险"]})
        if not feasible:
            result["reasons"].append("限价内份额或参考预算不足最低数量")
    except (ValueError, KeyError, TypeError, ArithmeticError) as error:
        result["reasons"].append(str(error))
    return result
