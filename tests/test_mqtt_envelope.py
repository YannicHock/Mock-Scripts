"""Tests fuer mqtt_envelope. Beruehrt keinen Broker."""

import json
import unittest

from mocks import mqtt_envelope as env


class EnvelopeTest(unittest.TestCase):
    def test_plan_message_hat_den_kopf_aus_dem_beispiel(self):
        msg = env.plan_message({"type": "plan", "assembly": "front_bumper"})
        self.assertEqual(msg["message-type"], "data")
        self.assertEqual(msg["publisher-id"], 123)
        self.assertEqual(msg["publisher-name"], "DFKI")
        self.assertEqual(msg["publisher-version"], 1)
        self.assertEqual(msg["publisher-description"], "plan description")
        self.assertIn("timestamp", msg)

    def test_plan_ist_ein_json_string_kein_objekt(self):
        msg = env.plan_message({"type": "plan", "assembly": "front_bumper"})
        self.assertIsInstance(msg["data"]["plan"], str)
        parsed = json.loads(msg["data"]["plan"])
        self.assertEqual(parsed["assembly"], "front_bumper")

    def test_action_message_hat_den_kopf_des_clients(self):
        msg = env.action_message("front_bumper", "abc123")
        self.assertEqual(msg["publisher-name"], "ifak")
        self.assertEqual(msg["publisher-description"], "action detection")
        self.assertEqual(msg["data"], {
            "type": "detectedAction",
            "assemblyID": "front_bumper",
            "actionID": "abc123",
        })

    def test_terminal_message_plan_complete(self):
        msg = env.terminal_message("planComplete", "front_bumper", ["a", "b"])
        self.assertEqual(msg["data"]["type"], "planComplete")
        self.assertEqual(msg["data"]["observed"], ["a", "b"])
        self.assertNotIn("reason", msg["data"])

    def test_terminal_message_no_matching_plan_mit_grund(self):
        msg = env.terminal_message("noMatchingPlan", "front_bumper", ["a"],
                                   reason="unbekannte actionID x_666")
        self.assertEqual(msg["data"]["type"], "noMatchingPlan")
        self.assertEqual(msg["data"]["reason"], "unbekannte actionID x_666")

    def test_read_data_liest_das_data_objekt(self):
        payload = json.dumps(env.action_message("a", "b")).encode("utf-8")
        self.assertEqual(env.read_data(payload)["actionID"], "b")

    def test_read_data_wirft_bei_kaputtem_json(self):
        with self.assertRaises(ValueError):
            env.read_data(b"{kein json")

    def test_read_data_wirft_ohne_data_feld(self):
        with self.assertRaises(ValueError):
            env.read_data(b'{"message-type":"data"}')

    def test_read_data_wirft_wenn_data_kein_objekt_ist(self):
        # Finding 5: {"data": "oops"} liess bisher den String durch - der
        # Aufrufer (data.get("type")) waere dann mit einem AttributeError
        # innerhalb des paho-Callbacks abgestuerzt, wo paho das schluckt.
        with self.assertRaises(ValueError):
            env.read_data(b'{"data": "oops"}')

    def test_timestamp_hat_offset(self):
        stamp = env.now_iso()
        self.assertTrue(stamp[-6] in "+-", stamp)
        self.assertEqual(stamp[-3], ":", stamp)


if __name__ == "__main__":
    unittest.main()
