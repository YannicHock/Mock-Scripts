"""Tests fuer die symbolischen Schraubloch-IDs im Konverter."""

import unittest
from pathlib import Path

from converter import resultat_to_json as conv

PLAN = Path("data/input/Resultat.txt")
ANNOTATIONS = Path("data/input/Stossecke_Li_Annotationen_1.lp")


class SchraublochIdsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ann = conv.parse_annotations(ANNOTATIONS.read_text(encoding="utf-8"))
        cls.plan = conv.build_plan(PLAN.read_text(encoding="utf-8"),
                                   "front_bumper", None, ann)
        cls.holes = [p for p in cls.plan["parts"] if p["type"] == "screwhole"]

    def test_jede_schraubloch_id_ist_symbolisch(self):
        for hole in self.holes:
            self.assertRegex(hole["aspID"], r"^\S+_screwhole_\d+$")

    def test_position_behaelt_die_koordinate(self):
        for hole in self.holes:
            self.assertTrue(hole["position"].startswith("POS: "))
            rest = hole["position"][len("POS: "):]
            self.assertRegex(rest, r"^-?\d+\.\d+ , -?\d+\.\d+ , -?\d+\.\d+$")

    def test_ids_sind_eindeutig(self):
        ids = [h["aspID"] for h in self.holes]
        self.assertEqual(len(ids), len(set(ids)))

    def test_bekannte_id_kommt_vor(self):
        ids = {h["aspID"] for h in self.holes}
        self.assertIn("A0038208256_A4108858714_screwhole_1", ids)
        self.assertIn("A4108804072_A0005405654_screwhole_4", ids)

    def test_substep_subjects_verwenden_die_symbolischen_ids(self):
        holes = {h["aspID"] for h in self.holes}
        hits = 0
        for block in self.plan["assembleSteps"]:
            for step in block["subSteps"]:
                subject = step.get("subject")
                if subject and "_screwhole_" in subject:
                    self.assertIn(subject, holes)
                    hits += 1
                if subject:
                    self.assertNotRegex(subject, r"^-?\d+\.\d+ , ")
        self.assertGreater(hits, 0)

    def test_substep_ids_bleiben_unveraendert(self):
        # Diese IDs stammen aus test_plan_1 3.json und sind SHA-256 ueber den
        # ASP-Rohfakt - die Umbenennung darf sie nicht anfassen.
        all_ids = {s["id"] for b in self.plan["assembleSteps"] for s in b["subSteps"]}
        for known in (
            "56bfba6811d059cd47028722f5dc9a043965373832c9587a3c09122cd3dcd62e",
            "b8face48a759cb401986b4abe663d75850782b03663b5c4f0f1bcf169e21ef79",
            "de10e5ef4ebbb4dc2d60cdafe3eaead1c395e0fb84f4db1d02caba0ba81670e1",
        ):
            self.assertIn(known, all_ids)

    def test_einfaches_format_bleibt_bei_koordinaten(self):
        # Ohne Annotationen gibt es keine Verbindungszuordnung, also auch keine
        # symbolischen IDs - dort bleibt die Koordinate im position-Feld.
        plan = conv.build_plan(PLAN.read_text(encoding="utf-8"), "x", None, None)
        self.assertNotIn("parts", plan)
        coordinates = [s.get("position") for b in plan["assembleSteps"]
                       for s in b["subSteps"] if s.get("position")]
        self.assertTrue(coordinates)
        self.assertRegex(coordinates[0], r"^-?\d+\.\d+ , ")


if __name__ == "__main__":
    unittest.main()
