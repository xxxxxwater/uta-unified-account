import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from decimal import MIN_EMIN, localcontext

from uta.liquidation import (ActionType, LiquidationController, Phase,
                             PositionReduction, TimedRisk)


@dataclass(frozen=True)
class Risk:
    equity: D
    maintenance_margin: D


class LiquidationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.controller = LiquidationController("acct")

    def snap(self, equity="100", mm="100", at=None, complete=True):
        return TimedRisk(Risk(D(equity), D(mm)), at or self.now, complete)

    def test_no_data_stale_and_invalid_are_blocked(self):
        for snap in (None, self.snap(at=self.now - timedelta(seconds=11)),
                     self.snap(at=self.now + timedelta(seconds=1)),
                     self.snap(complete=False), self.snap("NaN"), self.snap(mm="-1")):
            self.assertIsNone(self.controller.next_action(snap, now=self.now))
            self.assertEqual(self.controller.phase, Phase.BLOCKED)
            self.assertFalse(self.controller.trading_allowed)

    def test_cancel_then_reduce_and_recover_with_new_snapshots(self):
        c = self.controller
        cancel = c.next_action(self.snap(), now=self.now)
        self.assertEqual(cancel.kind, ActionType.CANCEL_ALL_ORDERS)
        self.assertEqual(c.next_action(self.snap(), now=self.now), cancel)
        self.assertFalse(c.acknowledge(cancel.action_id, success=False, now=self.now))
        self.assertTrue(c.acknowledge(cancel.action_id, success=True, now=self.now))
        self.assertFalse(c.acknowledge(cancel.action_id, success=True, now=self.now))
        # Snapshot before the acknowledgement cannot be used for a new action.
        self.assertIsNone(c.next_action(self.snap(), now=self.now))
        t = self.now + timedelta(seconds=1)
        reduction = PositionReduction("BTC-PERP", D("-2"), D("0.5"))
        action = c.next_action(self.snap(at=t), now=t, reduction=reduction)
        self.assertEqual((action.kind, action.side, action.quantity, action.reduce_only),
                         (ActionType.REDUCE_POSITION, "buy", D("0.5"), True))
        self.assertTrue(c.acknowledge(action.action_id, success=True, now=t))
        t2 = t + timedelta(seconds=1)
        self.assertIsNone(c.next_action(self.snap("111", at=t2), now=t2))
        self.assertEqual(c.phase, Phase.RECOVERED)
        self.assertTrue(c.trading_allowed)

    def test_recovery_buffer_and_position_bounds(self):
        c = self.controller
        a = c.next_action(self.snap(), now=self.now)
        c.acknowledge(a.action_id, success=True, now=self.now)
        t = self.now + timedelta(seconds=1)
        self.assertIsNone(c.next_action(self.snap("105", at=t), now=t))
        self.assertEqual(c.phase, Phase.REDUCE_EXPOSURE)
        self.assertFalse(c.trading_allowed)
        with self.assertRaises(ValueError):
            c.next_action(self.snap("105", at=t), now=t,
                          reduction=PositionReduction("BTC", D("1"), D("2")))

    def test_recovery_threshold_uses_exact_product(self):
        c = self.controller
        cancel = c.next_action(self.snap(), now=self.now)
        c.acknowledge(cancel.action_id, success=True, now=self.now)
        t = self.now + timedelta(seconds=1)

        # The exact threshold is 101.2 at this maintenance margin.
        with localcontext() as ctx:
            ctx.prec = 2
            for equity in ("101.19", "101.2"):
                with self.subTest(equity=equity):
                    self.assertIsNone(c.next_action(self.snap(equity, "92", at=t), now=t))
                    self.assertEqual(c.phase, Phase.REDUCE_EXPOSURE)
                    self.assertFalse(c.trading_allowed)
            self.assertIsNone(c.next_action(self.snap("101.201", "92", at=t), now=t))
        self.assertEqual(c.phase, Phase.RECOVERED)
        self.assertTrue(c.trading_allowed)

    def test_reduction_uses_exact_absolute_position_size(self):
        c = self.controller
        cancel = c.next_action(self.snap(), now=self.now)
        c.acknowledge(cancel.action_id, success=True, now=self.now)
        t = self.now + timedelta(seconds=1)

        with localcontext() as ctx:
            ctx.prec = 2
            with self.assertRaisesRegex(ValueError, "invalid position reduction"):
                c.next_action(self.snap(at=t), now=t,
                              reduction=PositionReduction("BTC", D("-1.96"), D("1.97")))
            action = c.next_action(self.snap(at=t), now=t,
                                   reduction=PositionReduction("BTC", D("-1.96"), D("1.96")))
        self.assertEqual((action.side, action.quantity, action.reduce_only), ("buy", D("1.96"), True))

    def test_recovery_threshold_below_context_exponent_range(self):
        c = self.controller
        cancel = c.next_action(self.snap(), now=self.now)
        c.acknowledge(cancel.action_id, success=True, now=self.now)
        t = self.now + timedelta(seconds=1)

        exponent = MIN_EMIN - 10
        margin = D((0, (1,), exponent))
        below_threshold = D((0, (1, 0, 9), exponent - 2))
        exact_threshold = D((0, (1, 1), exponent - 1))
        above_threshold = D((0, (1, 1, 1), exponent - 2))

        for equity in (margin, below_threshold, exact_threshold):
            with self.subTest(equity=equity):
                self.assertIsNone(c.next_action(self.snap(equity, margin, at=t), now=t))
                self.assertEqual(c.phase, Phase.REDUCE_EXPOSURE)
                self.assertFalse(c.trading_allowed)
        self.assertIsNone(c.next_action(self.snap(above_threshold, margin, at=t), now=t))
        self.assertEqual(c.phase, Phase.RECOVERED)
        self.assertTrue(c.trading_allowed)

    def test_stale_data_does_not_discard_pending_action(self):
        c = self.controller
        a = c.next_action(self.snap(), now=self.now)
        later = self.now + timedelta(seconds=20)
        self.assertIsNone(c.next_action(self.snap(), now=later))
        self.assertEqual(c.phase, Phase.BLOCKED)
        self.assertEqual(c.next_action(self.snap(at=later), now=later), a)
        self.assertFalse(c.trading_allowed)

    def test_unknown_ack_fails_and_healthy_can_trade(self):
        c = self.controller
        self.assertIsNone(c.next_action(self.snap("120"), now=self.now))
        self.assertTrue(c.trading_allowed)
        with self.assertRaises(ValueError):
            c.acknowledge("unknown", success=True, now=self.now)

if __name__ == '__main__':
    unittest.main()
