"""Tests fuer die Zustandsmaschine des Reasoners. Beruehrt keinen Broker."""

import json
import random
import unittest
from pathlib import Path

from mocks import plan_space
from mocks.or_reasoner_mock import Reasoner

TEST_PLAN = Path("data/plans/test_plan_1_3.json")


def make_reasoner(seed=1, initial_first_plan=False):
    space = plan_space.PlanSpace(
        plan_space.strip_null_subjects(plan_space.load_plan(TEST_PLAN)))
    return space, Reasoner(space, "front_bumper", random.Random(seed),
                           initial_first_plan=initial_first_plan)


def plan_of(msg):
    return json.loads(msg["data"]["plan"])


class ReasonerTest(unittest.TestCase):
    def test_start_liefert_eine_plan_nachricht(self):
        _, r = make_reasoner()
        msg = r.start()
        self.assertIn("plan", msg["data"])
        self.assertEqual(plan_of(msg)["assembly"], "front_bumper")
        self.assertFalse(r.finished)

    def test_start_mit_initial_first_plan_waehlt_initialen_plan(self):
        space, r = make_reasoner(initial_first_plan=True)
        r.start()
        self.assertEqual(r.current, space.initial_key())
        self.assertEqual(r.current, plan_space.PlanKey(order=(0, 1, 2, 3), layouts=(0, 0, 0, 0)))

    def test_start_ist_mit_seed_reproduzierbar(self):
        _, a = make_reasoner(5)
        _, b = make_reasoner(5)
        self.assertEqual(plan_of(a.start()), plan_of(b.start()))

    def test_vollstaendiger_durchlauf_endet_in_plan_complete(self):
        space, r = make_reasoner()
        r.start()
        sequence = space.flat_ids(plan_space.PlanKey(order=(0, 1, 2, 3),
                                                      layouts=(0, 0, 0, 0)))
        for action_id in sequence[:-1]:
            msg = r.on_action(action_id)
            self.assertIn("plan", msg["data"], action_id)
            self.assertFalse(r.finished)
        msg = r.on_action(sequence[-1])
        self.assertEqual(msg["data"]["type"], "planComplete")
        self.assertEqual(msg["data"]["observed"], list(sequence))
        self.assertTrue(r.finished)

    def test_gleicher_plan_wird_erneut_gesendet_solange_er_passt(self):
        space, r = make_reasoner()
        r.start()
        before = r.current
        sequence = space.flat_ids(before)
        r.on_action(sequence[0])
        self.assertEqual(r.current, before)

    def test_planwechsel_wenn_der_aktuelle_nicht_mehr_passt(self):
        space, r = make_reasoner()
        r.start()
        before = r.current
        sequence = space.flat_ids(before)
        # Einen ersten Step waehlen, der zu einem ANDEREN Plan gehoert.
        other_key = next(k for k in space.keys()
                         if space.flat_ids(k)[0] != sequence[0])
        first_id = space.flat_ids(other_key)[0]
        msg = r.on_action(first_id)
        self.assertIn("plan", msg["data"])
        self.assertNotEqual(r.current, before)
        self.assertTrue(space.matches(r.current, [first_id]))

    def test_blocksprung_endet_in_no_matching_plan(self):
        space, r = make_reasoner()
        r.start()
        r.on_action("de10e5ef4ebbb4dc2d60cdafe3eaead1c395e0fb84f4db1d02caba0ba81670e1")
        r.on_action("d711d88c0baf60d5d5fa1371ebd1f2c613e6bc2327af9e082d701e2cb4aaa0f5")
        msg = r.on_action("56bfba6811d059cd47028722f5dc9a043965373832c9587a3c09122cd3dcd62e")
        self.assertEqual(msg["data"]["type"], "noMatchingPlan")
        self.assertTrue(r.finished)

    def test_unbekannte_id_endet_in_no_matching_plan(self):
        _, r = make_reasoner()
        r.start()
        msg = r.on_action("x_666")
        self.assertEqual(msg["data"]["type"], "noMatchingPlan")
        self.assertIn("x_666", msg["data"]["reason"])
        self.assertTrue(r.finished)

    def test_doppelter_step_endet_in_no_matching_plan(self):
        space, r = make_reasoner()
        r.start()
        first_id = space.flat_ids(r.current)[0]
        r.on_action(first_id)
        msg = r.on_action(first_id)
        self.assertEqual(msg["data"]["type"], "noMatchingPlan")
        self.assertTrue(r.finished)

    def test_nach_dem_ende_wird_nicht_weiterverarbeitet(self):
        _, r = make_reasoner()
        r.start()
        r.on_action("x_666")
        with self.assertRaises(RuntimeError):
            r.on_action("egal")


if __name__ == "__main__":
    unittest.main()


class DecisionTest(unittest.TestCase):
    """Die Zustandsmaschine legt ihre Entscheidung offen, statt sie nur zu tun.

    Ohne das liesse sich von aussen nur aus dem Wechsel von `current` erraten,
    ob ein Plan behalten oder ersetzt wurde - und der Szenarienlauf koennte
    einen Planwechsel nicht als solchen ausgeben.
    """

    def setUp(self):
        self.space, self.reasoner = make_reasoner(1)

    def test_start_meldet_chosen_und_plan_eins(self):
        self.reasoner.start()
        self.assertEqual(self.reasoner.decision, "chosen")
        self.assertEqual(self.reasoner.plan_number, 1)
        self.assertEqual(self.reasoner.switches, 0)

    def test_passender_step_behaelt_den_plan(self):
        self.reasoner.start()
        sequence = self.space.flat_ids(self.reasoner.current)
        self.reasoner.on_action(sequence[0])
        self.assertEqual(self.reasoner.decision, "kept")
        self.assertEqual(self.reasoner.plan_number, 1)
        self.assertEqual(self.reasoner.switches, 0)

    def test_unpassender_step_erzwingt_einen_wechsel(self):
        self.reasoner.start()
        current = self.reasoner.current
        other = next(key for key in self.space.keys()
                     if self.space.flat_ids(key)[0] != self.space.flat_ids(current)[0])
        self.reasoner.on_action(self.space.flat_ids(other)[0])
        self.assertEqual(self.reasoner.decision, "switched")
        self.assertEqual(self.reasoner.plan_number, 2)
        self.assertEqual(self.reasoner.switches, 1)
        self.assertNotEqual(self.reasoner.current, current)

    def test_vollstaendiger_durchlauf_meldet_complete(self):
        self.reasoner.start()
        for action_id in self.space.flat_ids(
                plan_space.PlanKey(order=(0, 1, 2, 3), layouts=(0, 0, 0, 0))):
            self.reasoner.on_action(action_id)
        self.assertEqual(self.reasoner.decision, "complete")

    def test_unbekannte_id_meldet_nomatch(self):
        self.reasoner.start()
        self.reasoner.on_action("x_666")
        self.assertEqual(self.reasoner.decision, "nomatch")

    def test_total_ist_die_zahl_der_substeps(self):
        self.assertEqual(self.reasoner.total, 23)
