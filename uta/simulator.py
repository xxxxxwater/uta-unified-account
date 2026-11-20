"""Persistent UTA trade simulator. Prices and fills are supplied by the lab operator."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import re

from .api import json_value
from .ledger import Ledger, LedgerError, Posting
from .migration import build_shadow
from .models import Account, AssetSpec, InstrumentSpec, Position, RiskInputError
from .portfolio import calculate_portfolio, position_im_bound
from .risk import _decimal_exact, _ratio
from .shadow_adapter import convert_shadow
from .store import AccountNotFound, ShardedStore, Store, StoreConflict, StoreError

ASSETS = {
    "USD": AssetSpec(Decimal("1"), Decimal("1")),
    "BTC": AssetSpec(Decimal("60000"), Decimal("0.8")),
    "ETH": AssetSpec(Decimal("3000"), Decimal("0.75")),
}
INSTRUMENTS = {
    "BTC-PERP": InstrumentSpec(Decimal("60000"), Decimal("0.10"), Decimal("0.06")),
    "ETH-PERP": InstrumentSpec(Decimal("3000"), Decimal("0.12"), Decimal("0.07")),
}
MARKET_TTL = timedelta(minutes=5)
LIQUIDATION_FEE = Fraction("0.002")
LIQUIDATION_SLIPPAGE = Fraction("0.005")
LOT_STEP = Fraction("0.000001")
FIELDS = {
    "set_prices": {"prices", "marks"},
    "mark_and_liquidate": {"prices", "marks"},
    "place_order": {"symbol", "quantity", "limit_price", "fee_rate"},
    "fill_order": {"order_id", "quantity", "price"},
    "cancel_order": {"order_id"},
    "liquidate": set(),
}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
DECIMAL = re.compile(r"[+-]?[0-9]{1,12}(?:\.[0-9]{1,6})?\Z")


class SimulatorError(ValueError):
    """The submitted operation violates simulator policy."""


def _id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise SimulatorError("IDs require 1–64 letters, digits, dots, underscores or dashes")
    return value


def _input(value, *, positive=False):
    if not isinstance(value, str) or not DECIMAL.fullmatch(value):
        raise SimulatorError("expected a decimal string with up to 12 integer and 6 fractional digits")
    result = Fraction(value)
    if positive and result <= 0:
        raise SimulatorError("value must be positive")
    return result


def _text(value: Fraction) -> str:
    return format(_decimal_exact(value), "f")


def _default_state(now):
    return {
        "as_of": now.isoformat(),
        "balances": {"USD": "10000", "BTC": "0.1"},
        "liabilities": {}, "positions": {}, "orders": {}, "lots": {}, "source": None,
        "prices": {name: str(spec.price) for name, spec in ASSETS.items()},
        "marks": {name: str(spec.mark_price) for name, spec in INSTRUMENTS.items()},
        "asset_factors": {name: str(spec.collateral_factor) for name, spec in ASSETS.items()},
        "instruments": {
            name: {"initial_margin_rate": str(spec.initial_margin_rate),
                   "maintenance_margin_rate": str(spec.maintenance_margin_rate)}
            for name, spec in INSTRUMENTS.items()
        },
    }


def _specs(state):
    assets = {name: AssetSpec(Decimal(price), Decimal(state["asset_factors"][name]))
              for name, price in state["prices"].items()}
    instruments = {name: InstrumentSpec(Decimal(mark), **{
        key: Decimal(value) for key, value in state["instruments"][name].items()
    }) for name, mark in state["marks"].items()}
    return assets, instruments


def _open_orders(state):
    return (order for order in state["orders"].values() if order["status"] == "OPEN")


def _reserve(order, instrument):
    quantity = Fraction(order["remaining"])
    price = Fraction(order["limit_price"])
    rate = Fraction(order["fee_rate"])
    mark = Fraction(instrument.mark_price)
    # For buys the adverse price is the highest permitted fill; for sells it
    # is the lowest permitted fill. This holds for fee rates strictly below 1.
    cost = quantity * (price - mark) + abs(quantity) * price * rate
    return abs(quantity) * Fraction(position_im_bound(order["symbol"], instrument)) + max(0, cost)


def _risk(state):
    assets, instruments = _specs(state)
    positions = {
        symbol: Position(symbol, Decimal(row["quantity"]), Decimal(row["entry_price"]),
                         Decimal(row["entry_notional"]))
        for symbol, row in state["positions"].items()
    }
    reserve = sum((_reserve(order, instruments[order["symbol"]]) for order in _open_orders(state)), Fraction(0))
    account = Account(
        balances={asset: Decimal(amount) for asset, amount in state["balances"].items()},
        liabilities={asset: Decimal(amount) for asset, amount in state["liabilities"].items()},
        positions=positions, open_order_margin=_decimal_exact(reserve),
    )
    return calculate_portfolio(account, assets, instruments)


def _health(state, risk, now):
    quote_time = datetime.fromisoformat(state["as_of"])
    if not timedelta(0) <= now - quote_time <= MARKET_TTL:
        return "STALE"
    if risk.equity <= 0:
        return "INSOLVENT"
    if risk.equity <= risk.maintenance_margin:
        return "BREACH"
    return "HEALTHY"


def _report(state, now, risk=None):
    risk = _risk(state) if risk is None else risk
    return {"state": state, "risk": json_value(risk), "health": _health(state, risk, now)}


def _trade(state, symbol, quantity, price, fee_rate):
    """FIFO lots preserve exact book cost across partial fills and reversals."""
    lots = state["lots"].setdefault(symbol, [])
    remaining, realized = quantity, Fraction(0)
    while lots and remaining and Fraction(lots[0]["quantity"]) * remaining < 0:
        lot = lots[0]
        old, entry = Fraction(lot["quantity"]), Fraction(lot["price"])
        closed = min(abs(old), abs(remaining)) * (1 if old > 0 else -1)
        realized += closed * (price - entry)
        remaining += closed
        if old == closed:
            lots.pop(0)
        else:
            lot["quantity"] = _text(old - closed)
    if remaining:
        if lots and Fraction(lots[-1]["price"]) == price:
            lots[-1]["quantity"] = _text(Fraction(lots[-1]["quantity"]) + remaining)
        else:
            if len(lots) >= 1000:
                raise SimulatorError("open lot count limit reached")
            lots.append({"quantity": _text(remaining), "price": _text(price)})
    if lots:
        size = sum(Fraction(lot["quantity"]) for lot in lots)
        cost = sum(Fraction(lot["quantity"]) * Fraction(lot["price"]) for lot in lots)
        state["positions"][symbol] = {
            "quantity": _text(size),
            "entry_price": str(_ratio(cost, size)),
            "entry_notional": _text(cost),
        }
    else:
        del state["lots"][symbol]
        state["positions"].pop(symbol, None)
    fee = abs(quantity) * price * fee_rate
    cash_delta = realized - fee
    cash = Fraction(state["balances"].get("USD", "0")) + cash_delta
    state["balances"]["USD"] = _text(max(cash, 0))
    if cash < 0:
        state["liabilities"]["USD"] = _text(Fraction(state["liabilities"].get("USD", "0")) - cash)

    postings = []
    for counterparty, amount in (("simulated_clearing", realized), ("fees", -fee)):
        if amount:
            postings.extend((Posting("user_net_usd", "USD", _decimal_exact(amount)),
                             Posting(counterparty, "USD", _decimal_exact(-amount))))
    if postings:
        event = Ledger({"USD": 18}, unit_digits=60).post("trade", postings)
        journal = [{"account": row.account, "asset": row.asset,
                    "amount": format(row.amount, "f")} for row in event.postings]
    else:
        journal = []
    return {"symbol": symbol, "quantity": _text(quantity), "price": _text(price),
            "realized_pnl": _text(realized), "fee": _text(fee), "journal": journal}


def _order(state, order_id):
    _id(order_id)
    order = state["orders"].get(order_id)
    if order is None or order["status"] != "OPEN":
        raise SimulatorError("order is missing or no longer open")
    return order


def _update_prices(state, command, now):
    for key in ("prices", "marks"):
        values = command[key]
        if not isinstance(values, dict) or values.keys() != state[key].keys():
            raise SimulatorError(f"{key} must contain the complete configured market snapshot")
        state[key] = {name: _text(_input(price, positive=True)) for name, price in values.items()}
    if state["prices"]["USD"] != "1":
        raise SimulatorError("USD is the fixed reporting unit")
    if now < datetime.fromisoformat(state["as_of"]):
        raise SimulatorError("market timestamp cannot go backwards")
    state["as_of"] = now.isoformat()
    return {"action": "prices_updated"}


def _place(state, command_id, command):
    symbol = command["symbol"]
    if not isinstance(symbol, str) or symbol not in state["instruments"]:
        raise SimulatorError("unknown instrument")
    quantity = _input(command["quantity"])
    price = _input(command["limit_price"], positive=True)
    fee = _input(command["fee_rate"])
    if not quantity or not 0 <= fee <= Fraction("0.01"):
        raise SimulatorError("quantity must be nonzero and fee rate must be within 0–1%")
    if sum(1 for _ in _open_orders(state)) >= 100:
        raise SimulatorError("open order count limit reached")
    state["orders"][command_id] = {
        "symbol": symbol, "quantity": _text(quantity), "remaining": _text(quantity),
        "limit_price": _text(price), "fee_rate": _text(fee), "status": "OPEN",
    }
    risk = _risk(state)
    if risk.available_margin < 0:
        raise SimulatorError("insufficient portfolio initial margin")
    return {"action": "order_placed", "order_id": command_id}, risk


def _fill(state, command):
    order = _order(state, command["order_id"])
    size = _input(command["quantity"], positive=True)
    price = _input(command["price"], positive=True)
    remaining = Fraction(order["remaining"])
    quantity = size if remaining > 0 else -size
    if size > abs(remaining) or quantity * (price - Fraction(order["limit_price"])) > 0:
        raise SimulatorError("fill exceeds order size or limit price")
    trade = _trade(state, order["symbol"], quantity, price, Fraction(order["fee_rate"]))
    order["remaining"] = _text(remaining - quantity)
    if remaining == quantity:
        order["status"] = "FILLED"
        order["closed_version"] = state["version"] + 1
    return {"action": "order_filled", "order_id": command["order_id"], "fills": [trade]}


def _liquidate(state, before):
    open_ids = []
    for order_id, order in sorted(state["orders"].items()):
        if order["status"] == "OPEN":
            order["status"] = "CANCELED"
            order["closed_version"] = state["version"] + 1
            open_ids.append(order_id)
    if open_ids:
        return state, {"action": "liquidation_cancel", "order_ids": open_ids}, _risk(state)

    if not state["positions"]:
        raise SimulatorError("no derivative positions remain; cash-only deficit requires resolution")

    def close(candidate, symbol, size):
        position = Fraction(candidate["positions"][symbol]["quantity"])
        quantity = -size if position > 0 else size
        mark = Fraction(candidate["marks"][symbol])
        price = mark * (1 - LIQUIDATION_SLIPPAGE if quantity < 0 else 1 + LIQUIDATION_SLIPPAGE)
        return _trade(candidate, symbol, quantity, price, LIQUIDATION_FEE)

    # Try each position independently. Closing a hedge can increase the
    # portfolio deficit, so rank candidates by post-trade equity minus MM.
    candidates = []
    for symbol, pos in sorted(state["positions"].items()):
        entire = abs(Fraction(pos["quantity"]))
        half = (entire // (2 * LOT_STEP)) * LOT_STEP
        for size in ((half, entire) if half else (entire,)):
            candidate = copy.deepcopy(state)
            fill = close(candidate, symbol, size)
            after = _risk(candidate)
            score = Fraction(after.equity) - Fraction(after.maintenance_margin)
            candidates.append((score, candidate, [fill], after))
    score, best, fills, after = max(candidates, key=lambda item: item[0])
    if score <= Fraction(before.equity) - Fraction(before.maintenance_margin):
        # A deeply insolvent or hedged book may have no improving single leg.
        # Execute an atomic simulated close-out and preserve any residual debt.
        best = state
        fills = [close(best, symbol, abs(Fraction(pos["quantity"])))
                 for symbol, pos in sorted(state["positions"].items())]
        after = _risk(best)
    return best, {"action": "liquidation_fill", "fills": fills}, after


def _mark_and_liquidate(state, command, now):
    _update_prices(state, command, now)
    risk = _risk(state)
    actions = []
    # Yield after eight atomic simulator steps if the account still needs
    # attention. The next command can continue against the resulting version.
    for _ in range(8):
        if _health(state, risk, now) == "HEALTHY" or (
            not state["positions"] and not any(_open_orders(state))
        ):
            break
        state, action, risk = _liquidate(state, risk)
        actions.append(action)
    pending = _health(state, risk, now) != "HEALTHY" and (
        bool(state["positions"]) or any(_open_orders(state))
    )
    return state, {"action": "market_risk_processed", "liquidation_actions": actions,
                   "liquidation_pending": pending}, risk


class Simulator:
    def __init__(self, path, *, clock=None, shards=1):
        if type(shards) is not int or not 1 <= shards <= 64:
            raise SimulatorError("shards must be an integer between 1 and 64")
        if shards == 1:
            self.store = Store(path)
        else:
            self.store = ShardedStore(path, shards=shards)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def bootstrap(self, account_id):
        now = self._clock()
        state = _default_state(now)
        _risk(state)
        return _report(self.store.create(_id(account_id), state), now)

    def import_classic(self, account_id, snapshot):
        now = self._clock()
        account_id = _id(account_id)
        try:
            shadow = build_shadow(snapshot, now=now, max_future_skew=timedelta(0))
            state = convert_shadow(shadow, asset_config=ASSETS, instruments=INSTRUMENTS)
            # Keep quantities and settlement money within the simulator's
            # supported scales. Preserve cost as exact signed notional.
            for value in (*state["balances"].values(), *state["liabilities"].values()):
                _input(value)
            state["lots"] = {}
            for symbol, row in state["positions"].items():
                size, price = _input(row["quantity"]), _input(row["entry_price"], positive=True)
                state["lots"][symbol] = [{"quantity": _text(size), "price": _text(price)}]
                row["entry_notional"] = _text(size * price)
            _risk(state)
        except (ValueError, InvalidOperation) as exc:
            raise SimulatorError(str(exc)) from exc
        return _report(self.store.create(account_id, state), now)

    def view(self, account_id):
        return _report(self.store.read(_id(account_id)), self._clock())

    def events(self, account_id):
        return self.store.events(_id(account_id))

    def verify_replay(self, account_id):
        """Replay a stable event prefix with the recorded decision times."""
        snapshot, events = self.store.read_for_replay(_id(account_id))
        state = events[0]["command"]["state"]
        for event in events[1:]:
            command, original = event["command"], event["result"]
            if event["expected_version"] != state["version"] or event["version"] != state["version"] + 1:
                raise SimulatorError("event sequence is inconsistent")
            recorded = datetime.fromisoformat(original["evaluated_at"])
            state, computed = self._transition(state, event["command_id"], command, now=recorded)
            state["version"] = computed["version"] = event["version"]
            if computed != original:
                raise SimulatorError(f"receipt differs at version {event['version']}")
        if state != snapshot:
            raise SimulatorError("snapshot differs from replay")
        return {"account_id": account_id, "version": state["version"], "events_verified": len(events)}

    def execute(self, account_id, command_id, expected_version, command):
        return self.store.apply(*self._prepare(account_id, command_id, expected_version, command))

    def _prepare(self, account_id, command_id, expected_version, command):
        account_id, command_id = _id(account_id), _id(command_id)
        command = copy.deepcopy(command)
        kind = command.get("type") if isinstance(command, dict) else None
        if not isinstance(kind, str) or kind not in FIELDS or command.keys() != FIELDS[kind] | {"type"}:
            raise SimulatorError("unrecognized command or incorrect fields")
        if type(expected_version) is not int or not 0 <= expected_version < 2**63:
            raise SimulatorError("expected_version must be a nonnegative 64-bit integer")

        def transition(state):
            try:
                return self._transition(state, command_id, command)
            except (LedgerError, RiskInputError, InvalidOperation) as exc:
                raise SimulatorError(str(exc)) from exc

        return account_id, command_id, expected_version, command, transition

    def execute_batch(self, items):
        """Submit a bounded batch of individually versioned commands."""
        if not isinstance(items, list) or not 1 <= len(items) <= 256:
            raise SimulatorError("batch requires 1–256 commands")
        prepared = []
        indexes = []
        results = [None] * len(items)
        for index, item in enumerate(items):
            try:
                if not isinstance(item, dict) or item.keys() != {
                    "account_id", "command_id", "expected_version", "command"
                }:
                    raise SimulatorError("invalid batch command envelope")
                prepared.append(self._prepare(**item))
                indexes.append(index)
            except SimulatorError as exc:
                results[index] = {"ok": False, "status": 400, "error": str(exc)}

        try:
            replies = self.store.apply_many(prepared)
        except StoreError as exc:
            replies = [exc] * len(prepared)
        for index, reply in zip(indexes, replies):
            if isinstance(reply, Exception):
                if isinstance(reply, AccountNotFound):
                    status, message = 404, "account not found"
                elif isinstance(reply, StoreConflict):
                    status, message = 409, str(reply)
                elif isinstance(reply, SimulatorError):
                    status, message = 400, str(reply)
                else:
                    status, message = 503, "account store unavailable"
                results[index] = {"ok": False, "status": status, "error": message}
            else:
                results[index] = {"ok": True, "receipt": reply}
        return results

    def _transition(self, state, command_id, command, *, now=None):
        now = now or self._clock()
        kind = command["type"]
        risk = None
        if kind == "set_prices":
            result = _update_prices(state, command, now)
        elif kind == "mark_and_liquidate":
            state, result, risk = _mark_and_liquidate(state, command, now)
        elif kind == "cancel_order":
            order = _order(state, command["order_id"])
            order.update({"status": "CANCELED", "closed_version": state["version"] + 1})
            result = {"action": "order_canceled", "order_id": command["order_id"]}
        else:
            before = _risk(state)
            health = _health(state, before, now)
            if health == "STALE":
                raise SimulatorError("market snapshot is stale; update prices and marks")
            if kind == "liquidate":
                if health == "HEALTHY":
                    raise SimulatorError("account is above maintenance margin")
                state, result, risk = _liquidate(state, before)
            elif health != "HEALTHY":
                raise SimulatorError("account is in liquidation territory")
            elif kind == "place_order":
                result, risk = _place(state, command_id, command)
            else:
                if before.available_margin < 0:
                    raise SimulatorError("reserved orders are undercollateralized; cancel and reprice")
                result = _fill(state, command)
                risk = _risk(state)
                if risk.available_margin < 0:
                    raise SimulatorError("fill violates portfolio initial margin")
        risk = _risk(state) if risk is None else risk
        # Receipts are durable; cap the window of terminal orders in snapshots.
        terminal = sorted((order["closed_version"], order_id) for order_id, order in state["orders"].items()
                          if order["status"] != "OPEN")
        for _, order_id in terminal[:-100]:
            del state["orders"][order_id]
        result.update({"risk": json_value(risk), "health": _health(state, risk, now),
                       "evaluated_at": now.isoformat()})
        return state, result
