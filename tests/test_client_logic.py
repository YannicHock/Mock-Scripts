"""Tests fuer die Replay-Logik des Clients. Beruehrt keinen Broker."""

import json
import tempfile
import unittest
from pathlib import Path

from mocks.assembly_client_mock import Replayer, load_scenario, read_plan_id


SCENARIO = {
    "scenario": "t",
    "description": "d",
    "assemblyID": "front_bumper",
    "expectation": "e",
    "steps": [{"actionID": "a"}, {"actionID": "b"}],
}


class LoadScenarioTest(unittest.TestCase):
    def test_liest_eine_datei(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text(json.dumps(SCENARIO), encoding="utf-8")
            self.assertEqual(load_scenario(path)["assemblyID"], "front_bumper")

    def test_wirft_ohne_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text(json.dumps({"assemblyID": "x"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_scenario(path)

    def test_wirft_ohne_assembly_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text(json.dumps({"steps": [{"actionID": "a"}]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_scenario(path)

    def test_wirft_wenn_ein_step_keine_action_id_hat(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text(json.dumps({"assemblyID": "x", "steps": [{}]}),
                            encoding="utf-8")
            with self.assertRaises(ValueError):
                load_scenario(path)


class ReplayerTest(unittest.TestCase):
    def test_liefert_die_steps_der_reihe_nach(self):
        r = Replayer(SCENARIO)
        self.assertEqual(r.next_action(), "a")
        self.assertEqual(r.next_action(), "b")
        self.assertIsNone(r.next_action())

    def test_exhausted_und_sent(self):
        r = Replayer(SCENARIO)
        self.assertFalse(r.exhausted)
        r.next_action()
        self.assertFalse(r.exhausted)
        r.next_action()
        self.assertTrue(r.exhausted)
        self.assertEqual(r.sent, ["a", "b"])

    def test_assembly_id_wird_durchgereicht(self):
        self.assertEqual(Replayer(SCENARIO).assembly_id, "front_bumper")


class ReadPlanIdTest(unittest.TestCase):
    """Der Client liest aus einer Plan-Nachricht nur die id - und wirft, wenn
    das nicht geht, statt still weiterzulaufen."""

    def test_liest_die_id(self):
        raw = json.dumps({"id": "front_bumper_plan_o0-1-2-3_l0-0-0-0"})
        self.assertEqual(read_plan_id(raw),
                         "front_bumper_plan_o0-1-2-3_l0-0-0-0")

    def test_wirft_bei_kaputtem_json(self):
        with self.assertRaises(ValueError) as fall:
            read_plan_id("{nicht json")
        self.assertIn("lesbares JSON", str(fall.exception))

    def test_wirft_wenn_der_plan_kein_string_ist(self):
        # json.loads wirft hier TypeError - der wird zu ValueError, damit der
        # Aufrufer nur eine Ausnahme kennen muss.
        with self.assertRaises(ValueError) as fall:
            read_plan_id({"id": "x"})
        self.assertIn("lesbares JSON", str(fall.exception))

    def test_wirft_wenn_der_plan_kein_objekt_ist(self):
        with self.assertRaises(ValueError) as fall:
            read_plan_id("[1, 2]")
        self.assertIn("kein Objekt, sondern list", str(fall.exception))

    def test_wirft_ohne_id(self):
        with self.assertRaises(ValueError) as fall:
            read_plan_id(json.dumps({"assembly": "x"}))
        self.assertIn("ohne id-Feld", str(fall.exception))


if __name__ == "__main__":
    unittest.main()
