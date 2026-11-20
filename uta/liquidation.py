"""In-memory, deterministic liquidation *simulation*; no exchange calls are made.

A caller must supply fresh risk calculations and execute/acknowledge proposed actions.
This module does not persist state or provide distributed exactly-once execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Context, Decimal, MAX_EMAX, MIN_EMIN
from enum import Enum
from typing import Protocol


class Risk(Protocol):
    equity: Decimal
    maintenance_margin: Decimal


class Phase(str, Enum):
    NORMAL = "normal"
    BLOCKED = "blocked"
    CANCEL_ORDERS = "cancel_orders"
    REDUCE_EXPOSURE = "reduce_exposure"
    RECOVERED = "recovered"


class ActionType(str, Enum):
    CANCEL_ALL_ORDERS = "cancel_all_orders"
    REDUCE_POSITION = "reduce_position"


@dataclass(frozen=True)
class TimedRisk:
    risk: Risk
    observed_at: datetime
    complete: bool = True


@dataclass(frozen=True)
class PositionReduction:
    symbol: str
    position_size: Decimal  # signed position, negative for short
    quantity: Decimal  # positive quantity; direction is derived from position_size


@dataclass(frozen=True)
class Action:
    action_id: str
    kind: ActionType
    symbol: str | None = None
    side: str | None = None
    quantity: Decimal | None = None
    reduce_only: bool = False


def _above_recovery_threshold(equity: Decimal, margin: Decimal, buffer: Decimal) -> bool:
    """Compare equity with margin times the buffer over the finite Decimal range."""
    if equity <= margin:
        return False
    if not margin:
        return True

    # Each positive significand is in [1, 10), so the product's adjusted
    # exponent is either the sum of the operand exponents or one higher.
    exponent_delta = equity.adjusted() - margin.adjusted() - buffer.adjusted()
    if not 0 <= exponent_delta <= 1:
        return exponent_delta > 1

    # Compare on the product's scale to keep extreme exponents out of
    # arithmetic. The product fits in the sum of the operand digit counts.
    margin_digits = margin.as_tuple().digits
    buffer_digits = buffer.as_tuple().digits
    equity_digits = equity.as_tuple().digits
    scaled_margin = Decimal((0, margin_digits, 1 - len(margin_digits)))
    scaled_buffer = Decimal((0, buffer_digits, 1 - len(buffer_digits)))
    scaled_equity = Decimal((0, equity_digits, exponent_delta + 1 - len(equity_digits)))
    context = Context(prec=len(margin_digits) + len(buffer_digits), Emin=MIN_EMIN, Emax=MAX_EMAX)
    return scaled_equity > context.multiply(scaled_margin, scaled_buffer)


class LiquidationController:
    """Single-account simulation. Serialize access and persist state externally.

    Liquidation starts at equity <= maintenance margin. Recovery requires equity
    > maintenance margin * recovery_buffer. Both are illustrative policy choices.
    A successful cancel acknowledgement means all relevant orders were confirmed
    cancelled by the caller. A successful reduction acknowledgement means it was
    confirmed; it is not a substitute for a newly calculated account snapshot.
    """

    def __init__(self, account_id: str, *, max_age: timedelta = timedelta(seconds=10),
                 recovery_buffer: Decimal = Decimal("1.10")) -> None:
        if not account_id or max_age <= timedelta(0):
            raise ValueError("account_id and a positive max_age are required")
        if not isinstance(recovery_buffer, Decimal) or not recovery_buffer.is_finite() or recovery_buffer < 1:
            raise ValueError("recovery_buffer must be a finite Decimal >= 1")
        self.account_id = account_id
        self.max_age = max_age
        self.recovery_buffer = recovery_buffer
        self.phase = Phase.BLOCKED  # no risk data means no trading
        self._active_phase = Phase.NORMAL
        self._cycle = 0
        self._sequence = 0
        self._pending: Action | None = None
        self._completed: set[str] = set()
        self._last_ack_at: datetime | None = None
        self._last_observed_at: datetime | None = None
        self._can_trade = False

    @property
    def trading_allowed(self) -> bool:
        """Last evaluation only; re-evaluate with current time before every order."""
        return self._can_trade

    @property
    def pending_action(self) -> Action | None:
        return self._pending

    def _valid(self, snapshot: TimedRisk | None, now: datetime) -> bool:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            return False
        if snapshot is None or not snapshot.complete or not isinstance(snapshot.observed_at, datetime):
            return False
        observed = snapshot.observed_at
        if observed.tzinfo is None or observed.utcoffset() is None:
            return False
        if observed > now or now - observed > self.max_age:
            return False
        # Never act on a snapshot older than one previously seen, or on a
        # snapshot taken before the preceding action was acknowledged.
        if self._last_observed_at is not None and observed < self._last_observed_at:
            return False
        if self._last_ack_at is not None and observed <= self._last_ack_at:
            return False
        try:
            equity, mm = snapshot.risk.equity, snapshot.risk.maintenance_margin
            return (isinstance(equity, Decimal) and isinstance(mm, Decimal)
                    and equity.is_finite() and mm.is_finite() and mm >= 0)
        except AttributeError:
            return False

    def next_action(self, snapshot: TimedRisk | None, *, now: datetime,
                    reduction: PositionReduction | None = None) -> Action | None:
        """Evaluate a fresh snapshot and return an action, or None.

        A pending action is returned unchanged until successfully acknowledged.
        An invalid snapshot blocks both trading and new liquidation actions; it
        never silently turns an unknown risk into a healthy account.
        """
        self._can_trade = False
        if not self._valid(snapshot, now):
            self.phase = Phase.BLOCKED
            return None
        assert snapshot is not None
        self._last_observed_at = snapshot.observed_at
        self.phase = self._active_phase
        if self._pending is not None:
            return self._pending
        equity, mm = snapshot.risk.equity, snapshot.risk.maintenance_margin
        if self._active_phase in (Phase.NORMAL, Phase.RECOVERED):
            if equity > mm:
                self.phase = self._active_phase = Phase.NORMAL
                self._can_trade = True
                return None
            self._cycle += 1
            self.phase = self._active_phase = Phase.CANCEL_ORDERS
        elif _above_recovery_threshold(equity, mm, self.recovery_buffer):
            self.phase = self._active_phase = Phase.RECOVERED
            self._can_trade = True
            return None

        self._sequence += 1
        action_id = f"{self.account_id}:{self._cycle}:{self._sequence}"
        if self._active_phase == Phase.CANCEL_ORDERS:
            self._pending = Action(action_id, ActionType.CANCEL_ALL_ORDERS)
        elif self._active_phase == Phase.REDUCE_EXPOSURE:
            if reduction is None:
                self._sequence -= 1
                return None
            if (not reduction.symbol or not isinstance(reduction.position_size, Decimal)
                    or not isinstance(reduction.quantity, Decimal)
                    or not reduction.position_size.is_finite() or not reduction.quantity.is_finite()
                    or reduction.position_size == 0 or reduction.quantity <= 0
                    or reduction.quantity > reduction.position_size.copy_abs()):
                self._sequence -= 1
                raise ValueError("invalid position reduction")
            side = "sell" if reduction.position_size > 0 else "buy"
            self._pending = Action(action_id, ActionType.REDUCE_POSITION,
                                   reduction.symbol, side, reduction.quantity, True)
        return self._pending

    def acknowledge(self, action_id: str, *, success: bool, now: datetime) -> bool:
        """Acknowledge a confirmed action. Return False for a repeated ack.

        Failure leaves the same action pending for retry. Callers must ensure the
        external venue uses the action ID as an idempotency key and reconcile
        ambiguous outcomes before retrying. This simulator does not do that.
        """
        if action_id in self._completed:
            return False
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("acknowledgement time must be timezone-aware")
        if self._last_observed_at is not None and now < self._last_observed_at:
            raise ValueError("acknowledgement cannot predate the risk snapshot")
        if self._pending is None or self._pending.action_id != action_id:
            raise ValueError("unknown or out-of-order action")
        self._can_trade = False
        if not success:
            return False
        self._completed.add(action_id)
        self._last_ack_at = now
        if self._pending.kind == ActionType.CANCEL_ALL_ORDERS:
            self.phase = self._active_phase = Phase.REDUCE_EXPOSURE
        self._pending = None
        return True
