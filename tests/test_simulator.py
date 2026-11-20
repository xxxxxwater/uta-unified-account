from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from uta.simulator import Simulator, SimulatorError
from uta.store import StoreConflict


class SimulatorTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "test.sqlite3"
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.sim = Simulator(self.path, clock=lambda: self.now)
        self.sim.bootstrap("account")

    def perform(self, command_id, **command):
        version = self.sim.view("account")["state"]["version"]
        return self.sim.execute("account", command_id, version, command)

    def place(self, order_id, qty, limit="60000", fee="0.001"):
        return self.perform(order_id, type="place_order", symbol="BTC-PERP",
                            quantity=qty, limit_price=limit, fee_rate=fee)

    def fill(self, order_id, fill_id, qty, price="60000"):
        return self.perform(fill_id, type="fill_order", order_id=order_id,
                            quantity=qty, price=price)

    def test_extreme_price_move_executes_liquidation_and_replays(self):
        self.place("long", "0.900001")
        self.fill("long", "open", "0.500001")
        self.fill("long", "open2", "0.4")
        self.place("pending-Z", "0.1")
        self.place("pending-A", "0.1")
        self.perform("shock", type="set_prices",
                     prices={"USD": "1", "BTC": "40000", "ETH": "2100"},
                     marks={"BTC-PERP": "40000.000001", "ETH-PERP": "2100"})
        self.assertIn(self.sim.view("account")["health"], ("BREACH", "INSOLVENT"))
        canceled = self.perform("cancel", type="liquidate")
        self.assertEqual(canceled["action"], "liquidation_cancel")
        self.assertEqual(canceled["order_ids"], ["pending-A", "pending-Z"])

        before = self.sim.view("account")["state"]
        version = before["version"]
        receipt = self.perform("liquidation", type="liquidate")
        self.assertEqual(receipt["action"], "liquidation_fill")
        self.assertTrue(receipt["fills"])
        for fill in receipt["fills"]:
            self.assertLess(Decimal(fill["quantity"]), 0)
            postings = fill["journal"]
            self.assertEqual(sum(Decimal(p["amount"]) for p in postings), 0)
            self.assertEqual(sum(Decimal(p["amount"]) for p in postings if p["account"] == "user_net_usd"),
                             Decimal(fill["realized_pnl"]) - Decimal(fill["fee"]))

        reopened = Simulator(self.path, clock=lambda: self.now)
        self.assertEqual(reopened.execute("account", "liquidation", version, {"type": "liquidate"}), receipt)
        self.assertEqual(reopened.verify_replay("account")["events_verified"], receipt["version"] + 1)
        after = reopened.view("account")["state"]
        self.assertEqual(after["positions"], {})
        self.assertGreater(Decimal(after["liabilities"]["USD"]), 0)

    def test_parallel_checks_cannot_admit_orders_using_same_margin(self):
        barrier = Barrier(2)
        command = {"type": "place_order", "symbol": "BTC-PERP",
                   "quantity": "1", "limit_price": "60000", "fee_rate": "0.001"}

        def place(order_id):
            barrier.wait(timeout=5)
            return self.sim.execute("account", order_id, 0, command)

        with ThreadPoolExecutor(2) as pool:
            attempts = [pool.submit(place, order_id) for order_id in ("racer-A", "racer-B")]
            results = [(attempt.result() if attempt.exception() is None else attempt.exception())
                       for attempt in attempts]
        self.assertEqual(sum(isinstance(result, StoreConflict) for result in results), 1)
        self.assertEqual(len(self.sim.events("account")), 2)
        with self.assertRaisesRegex(SimulatorError, "insufficient"):
            self.sim.execute("account", "after-race", 1, command)
        self.assertEqual(len(self.sim.events("account")), 2)

    def test_stale_quotes_block_fills_and_invalid_limit_preserves_reservation(self):
        self.place("buy", "0.2")
        with self.assertRaisesRegex(SimulatorError, "limit"):
            self.fill("buy", "worse", "0.1", "60000.01")
        self.assertEqual(self.sim.view("account")["state"]["orders"]["buy"]["remaining"], "0.2")
        self.now += timedelta(minutes=6)
        self.assertEqual(self.sim.view("account")["health"], "STALE")
        with self.assertRaisesRegex(SimulatorError, "stale"):
            self.fill("buy", "stale", "0.1")
        self.perform("cancel", type="cancel_order", order_id="buy")
        self.assertEqual(self.sim.view("account")["risk"]["open_order_margin"], "0")

    def test_market_shock_cancels_and_settles_in_one_command(self):
        self.place("long", "0.9")
        self.fill("long", "entry", "0.9")
        self.place("pending", "0.1")
        receipt = self.perform("scheduled-shock", type="mark_and_liquidate",
                               prices={"USD": "1", "BTC": "40000", "ETH": "2100"},
                               marks={"BTC-PERP": "40000", "ETH-PERP": "2100"})
        self.assertEqual([action["action"] for action in receipt["liquidation_actions"]],
                         ["liquidation_cancel", "liquidation_fill"])
        self.assertFalse(receipt["liquidation_pending"])
        self.assertFalse(self.sim.view("account")["state"]["positions"])
        self.assertEqual(self.sim.verify_replay("account")["events_verified"], 5)

    def test_replay_verifies_its_captured_version_during_another_commit(self):
        self.place("buy", "0.1")
        writer = Simulator(self.path, clock=lambda: self.now)
        read_account = self.sim.store._account

        def read_with_concurrent_commit(db, account_id):
            row = read_account(db, account_id)
            if row["version"] == 1:
                writer.execute("account", "cancel", 1, {"type": "cancel_order", "order_id": "buy"})
            return row

        with patch.object(self.sim.store, "_account", side_effect=read_with_concurrent_commit):
            self.assertEqual(self.sim.verify_replay("account"),
                             {"account_id": "account", "version": 1, "events_verified": 2})

        self.assertEqual(self.sim.verify_replay("account"),
                         {"account_id": "account", "version": 2, "events_verified": 3})

    def test_fifo_partial_fills_and_reversal_keep_exact_cost(self):
        self.place("buy-A", "0.03", "60000", "0")
        self.fill("buy-A", "fill-A", "0.03")
        self.place("buy-B", "0.07", "60003", "0")
        self.fill("buy-B", "fill-B", "0.07", "60003")
        self.place("sell", "-0.11", "60005", "0")
        first = self.fill("sell", "sell-first", "0.04", "60005")
        self.assertEqual(first["fills"][0]["realized_pnl"], "0.17")
        self.assertEqual(self.sim.view("account")["state"]["positions"]["BTC-PERP"]["entry_notional"], "3600.18")
        second = self.fill("sell", "sell-rest", "0.07", "60005")
        self.assertEqual(second["fills"][0]["realized_pnl"], "0.12")
        position = self.sim.view("account")["state"]["positions"]["BTC-PERP"]
        self.assertEqual((position["quantity"], position["entry_notional"]), ("-0.01", "-600.05"))

    def test_classic_seed_preserves_gross_balances_and_lot_cost(self):
        def balance(asset, free, borrowed):
            return {"asset": asset, "free": free, "locked": "0", "borrowed": borrowed, "interest": "0.001"}
        view = self.sim.import_classic("imported", {
            "account_id": "old", "version": 12, "captured_at": self.now.isoformat(),
            "compartments": {
                "spot": {"balances": [balance("BTC", "0.25", "0.05")]},
                "margin": {"balances": []},
                "futures": {"balances": [], "positions": [
                    {"symbol": "BTC-PERP", "quantity": "-0.1", "entry_price": "6e4"},
                ]},
            },
        })
        self.assertEqual(view["state"]["source"]["version"], 12)
        self.assertEqual(view["state"]["balances"]["BTC"], "0.25")
        self.assertEqual(view["state"]["liabilities"]["BTC"], "0.051")
        self.assertEqual(view["state"]["positions"]["BTC-PERP"]["entry_notional"], "-6000")
        self.assertEqual(self.sim.verify_replay("imported")["events_verified"], 1)


if __name__ == "__main__":
    unittest.main()
