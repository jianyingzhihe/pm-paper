"""逐档模拟成交、费用及正式结果核算。"""
from decimal import Decimal, ROUND_DOWN
import json

ZERO = Decimal(0)
CENT = Decimal("0.01")

def D(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("数值必须有限")
    return result


def fee_rate(snapshot, mid):
    fee = (snapshot.get("fees", {}).get(mid) or {}).get("fd") or {}
    if fee.get("r") is None or fee.get("e") is None or D(fee["e"]) != 1:
        return None
    rate = D(fee["r"])
    return rate if ZERO <= rate <= 1 else None


def levels(book, side, fraction=Decimal(1)):
    answer = []
    for level in book.get(side) or []:
        price, size = D(level["price"]), D(level["size"]) * fraction
        if 0 < price < 1 and size > 0:
            answer.append((price, size))
    return sorted(answer, reverse=side == "bids")


def sell_value(book, quantity, rate, fraction=Decimal(1), limit=ZERO):
    left, gross, fees = quantity, ZERO, ZERO
    fills = []
    for price, size in levels(book, "bids", fraction):
        if price < limit:
            break
        take = min(left, size).quantize(CENT, rounding=ROUND_DOWN)
        if take <= 0:
            continue
        gross += take * price
        fees += take * rate * price * (1 - price)
        left -= take
        fills.append({"price": str(price), "quantity": str(take)})
        if left <= 0:
            break
    return {"filled": quantity - left, "unfilled": left, "net": gross - fees, "fee": fees, "fills": fills}


def buy_value(book, budget, rate, limit, fraction):
    left, quantity, total_fee = budget, ZERO, ZERO
    fills = []
    for price, size in levels(book, "asks", fraction):
        if price > limit:
            break
        unit_cost = price + rate * price * (1 - price)
        take = min(size, left / unit_cost).quantize(CENT, rounding=ROUND_DOWN)
        if take <= 0:
            continue
        cost = take * unit_cost
        left -= cost
        quantity += take
        total_fee += take * rate * price * (1 - price)
        fills.append({"price": str(price), "quantity": str(take)})
    return {"filled": quantity, "cost": budget - left, "fee": total_fee, "fills": fills}


def final_winner(state):
    gamma, clob = state.get("gamma", {}), state.get("clob", {})
    if not (gamma.get("closed") and gamma.get("umaResolutionStatus") == "resolved" and clob.get("closed")):
        return None
    if not gamma.get("conditionId") or gamma["conditionId"] != clob.get("condition_id"):
        return None
    tokens = clob.get("tokens") or []
    if any(not isinstance(token.get("winner"), bool) for token in tokens):
        return None
    winners = [token for token in tokens if token.get("winner")]
    if len(tokens) != 2 or len(winners) != 1:
        return None
    if D(winners[0].get("price", -1)) != 1:
        return None
    if any(D(token.get("price", -1)) != 0 for token in tokens if not token.get("winner")):
        return None
    outcomes = gamma.get("outcomes") or []
    prices = gamma.get("outcomePrices") or []
    outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
    prices = json.loads(prices) if isinstance(prices, str) else prices
    if len(outcomes) != 2 or len(prices) != 2 or len(set(outcomes)) != 2:
        return None
    values = [D(price) for price in prices]
    if sorted(values) != [ZERO, Decimal(1)]:
        return None
    winner = outcomes[values.index(Decimal(1))]
    return winner if winner == winners[0]["outcome"] else None
