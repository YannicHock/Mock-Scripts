"""Tests fuer plan_space."""

import json
import random
import tempfile
import unittest
from pathlib import Path

from mocks import plan_space

TEST_PLAN = Path("data/plans/test_plan_1_3.json")


class LoadPlanTest(unittest.TestCase):
    def test_load_plan_liest_den_testplan(self):
        plan = plan_space.load_plan(TEST_PLAN)
        self.assertEqual(plan["assembly"], "front_bumper")
        self.assertEqual(len(plan["assembleSteps"]), 4)

    def test_strip_null_subjects_laesst_testplan_unveraendert(self):
        plan = plan_space.load_plan(TEST_PLAN)
        cleaned = plan_space.strip_null_subjects(plan)
        before = sum(len(s["subSteps"]) for s in plan["assembleSteps"])
        after = sum(len(s["subSteps"]) for s in cleaned["assembleSteps"])
        self.assertEqual(before, 23)
        self.assertEqual(after, 23)

    def test_strip_null_subjects_entfernt_und_kopiert(self):
        plan = {
            "type": "plan",
            "assembly": "x",
            "assembleSteps": [
                {
                    "id": "a",
                    "connection": "c",
                    "step": "0",
                    "subSteps": [
                        {"id": "s1", "actionType": "position", "subject": None,
                         "object": "o", "step": "1"},
                        {"id": "s2", "actionType": "insert", "subject": "hole",
                         "object": "o", "step": "2"},
                        {"id": "s3", "actionType": "insert", "subject": "none",
                         "object": "o", "step": "3"},
                        {"id": "s4", "actionType": "insert", "subject": "",
                         "object": "o", "step": "4"},
                    ],
                }
            ],
        }
        cleaned = plan_space.strip_null_subjects(plan)
        ids = [s["id"] for s in cleaned["assembleSteps"][0]["subSteps"]]
        self.assertEqual(ids, ["s2"])
        # Original bleibt unberuehrt
        self.assertEqual(len(plan["assembleSteps"][0]["subSteps"]), 4)


class LayoutsTest(unittest.TestCase):
    def setUp(self):
        plan = plan_space.strip_null_subjects(plan_space.load_plan(TEST_PLAN))
        self.blocks = plan["assembleSteps"]

    def test_anzahl_je_block(self):
        counts = [len(plan_space.layouts_for(b["subSteps"])) for b in self.blocks]
        self.assertEqual(counts, [1, 576, 1, 10])

    def test_layout_enthaelt_genau_dieselben_substeps(self):
        for block in self.blocks:
            original = sorted(s["id"] for s in block["subSteps"])
            for layout in plan_space.layouts_for(block["subSteps"]):
                self.assertEqual(sorted(s["id"] for s in layout), original)

    def test_layouts_sind_paarweise_verschieden(self):
        for block in self.blocks:
            layouts = plan_space.layouts_for(block["subSteps"])
            seen = {tuple(s["id"] for s in lay) for lay in layouts}
            self.assertEqual(len(seen), len(layouts))

    def test_erstes_layout_ist_das_original(self):
        for block in self.blocks:
            first = plan_space.layouts_for(block["subSteps"])[0]
            self.assertEqual([s["id"] for s in first],
                             [s["id"] for s in block["subSteps"]])

    def test_tighten_immer_hinter_turnon_derselben_koordinate(self):
        for block in self.blocks:
            for layout in plan_space.layouts_for(block["subSteps"]):
                pos = {}
                for i, s in enumerate(layout):
                    pos.setdefault((s["actionType"], s["subject"]), i)
                for s in layout:
                    if s["actionType"] != "tighten":
                        continue
                    turnon = pos.get(("turnon", s["subject"]))
                    if turnon is not None:
                        self.assertLess(turnon, pos[("tighten", s["subject"])],
                                        f"tighten vor turnon in {block['connection']}")

    def test_letztes_layout_von_block_3_ist_vollstaendig_gruppiert(self):
        # Pin fuer Finding 4: layouts_for() haengt Layout B hinter Layout A an
        # (siehe _expand(_runs(...)) + _expand(_grouped(...)) dort), das
        # letzte Layout ist also immer aus Layout B. Wuerden die beiden
        # _expand-Aufrufe vertauscht, wuerde Szenario 02 (das genau dieses
        # letzte Layout je Block nimmt, siehe _last_layout_index) still etwas
        # anderes testen, ohne dass ein anderer Test das merkt.
        block = next(b for b in self.blocks
                     if b["connection"] == "A0038208256_A4108858814")
        last = plan_space.layouts_for(block["subSteps"])[-1]
        self.assertEqual(
            [s["actionType"] for s in last],
            ["position", "insert", "insert", "turnon", "turnon",
             "tighten", "tighten"])

    def test_gruppierung_kommt_vor(self):
        # Block A0038208256_A4108858814 hat posi,inse,turn,inse,turn,tigh,tigh.
        # Layout B gruppiert zu posi | inse,inse | turn,turn | tigh,tigh - darin
        # steht der zweite insert direkt hinter dem ersten.
        block = self.blocks[3]
        type_sequences = {tuple(s["actionType"] for s in lay)
                     for lay in plan_space.layouts_for(block["subSteps"])}
        self.assertIn(
            ("position", "insert", "insert", "turnon", "turnon", "tighten", "tighten"),
            type_sequences)


def _synthetic_nuts_plan(n, connection="SYN"):
    """Ein Block mit n turnon- und n tighten-Substeps auf einer Verbindung -
    dieselbe Form, die den Testplan von 60 auf 138240 Plaene brachte
    (Finding 2 / Spec 3.3: "ein weiterer Step mit 4 Muttern").
    """
    subs = []
    for i in range(n):
        subs.append({"id": f"t{i}", "actionType": "turnon", "subject": f"n{i}",
                     "object": "o", "step": str(i + 1)})
    for i in range(n):
        subs.append({"id": f"g{i}", "actionType": "tighten", "subject": f"n{i}",
                     "object": "o", "step": str(n + i + 1)})
    return {
        "type": "plan", "assembly": "synthetic", "components": [], "parts": [],
        "assembleSteps": [{"id": "b0", "connection": connection, "step": "0",
                           "subSteps": subs}],
    }


class LayoutCeilingTest(unittest.TestCase):
    """Finding 2: PlanSpace muss vor der Allokation abbrechen, nicht danach."""

    def test_ueber_der_default_ceiling_wird_vor_der_expansion_verweigert(self):
        # 7 Muttern: geschaetzte Layoutzahl liegt weit ueber der Default-
        # Ceiling. Der Check ist reine Arithmetik (Fakultaeten), _expand()
        # wird nie aufgerufen - der Test bleibt trotzdem sofort fertig, obwohl
        # die reale Expansion laut Review 377s/15GB braeuchte.
        plan = _synthetic_nuts_plan(7)
        with self.assertRaises(ValueError) as ctx:
            plan_space.PlanSpace(plan)
        self.assertIn("SYN", str(ctx.exception))

    def test_kleine_bloecke_gehen_mit_der_default_ceiling_durch(self):
        # 4 Muttern entspricht genau dem groessten Block im Testplan (576
        # Layouts) - darf unter der Default-Ceiling nicht abgelehnt werden.
        plan = _synthetic_nuts_plan(4)
        space = plan_space.PlanSpace(plan)
        self.assertEqual(space.count(), 576)

    def test_caller_kann_die_ceiling_explizit_anheben(self):
        # 5 Muttern (14400 Layouts, laut Review 0.14s) wird mit einer engen
        # Ceiling abgelehnt, aber mit einer bewusst angehobenen zugelassen -
        # "Put the ceiling where a caller can raise it deliberately".
        plan = _synthetic_nuts_plan(5)
        with self.assertRaises(ValueError):
            plan_space.PlanSpace(plan, layout_ceiling=1000)
        space = plan_space.PlanSpace(plan, layout_ceiling=50_000)
        self.assertEqual(space.count(), 14400)

    def test_shipped_plan_bleibt_unter_der_default_ceiling(self):
        # Der ausgelieferte Testplan darf durch Finding 2 nicht beruehrt
        # werden - Layoutzahlen bleiben [1, 576, 1, 10].
        plan = plan_space.strip_null_subjects(plan_space.load_plan(TEST_PLAN))
        space = plan_space.PlanSpace(plan)
        self.assertEqual([len(lay) for lay in space.layouts], [1, 576, 1, 10])
        self.assertEqual(space.count(), 138240)


class DependencyCheckTest(unittest.TestCase):
    """Finding 6: turnon vor tighten wird zur Konstruktionszeit erzwungen."""

    def test_plan_mit_vertauschter_reihenfolge_wird_abgelehnt(self):
        plan = {
            "type": "plan", "assembly": "x", "components": [], "parts": [],
            "assembleSteps": [{
                "id": "b0", "connection": "BAD", "step": "0",
                "subSteps": [
                    {"id": "s1", "actionType": "tighten", "subject": "n1",
                     "object": "o", "step": "1"},
                    {"id": "s2", "actionType": "turnon", "subject": "n1",
                     "object": "o", "step": "2"},
                ],
            }],
        }
        with self.assertRaises(ValueError) as ctx:
            plan_space.PlanSpace(plan)
        self.assertIn("BAD", str(ctx.exception))

    def test_beide_reale_plaene_bestehen_die_pruefung(self):
        # Vom Reviewer bestaetigt: die Eigenschaft haelt auf beiden echten
        # Plaenen im Repo. Die Ergaenzung darf nichts ablehnen, was heute
        # funktioniert.
        for path in (TEST_PLAN, Path("data/output/Resultat_output_2.json")):
            plan = plan_space.strip_null_subjects(plan_space.load_plan(path))
            plan_space.PlanSpace(plan)  # darf nicht werfen


class PlanSpaceTest(unittest.TestCase):
    def setUp(self):
        self.space = plan_space.PlanSpace(
            plan_space.strip_null_subjects(plan_space.load_plan(TEST_PLAN)))

    def test_count(self):
        self.assertEqual(self.space.count(), 138240)

    def test_initial_key(self):
        self.assertEqual(self.space.initial_key(),
                         plan_space.PlanKey(order=(0, 1, 2, 3), layouts=(0, 0, 0, 0)))

    def test_keys_liefert_genau_count_schluessel(self):
        self.assertEqual(sum(1 for _ in self.space.keys()), 138240)

    def test_alle_flachen_folgen_sind_verschieden(self):
        seen = {self.space.flat_ids(k) for k in self.space.keys()}
        self.assertEqual(len(seen), 138240)

    def test_flache_folge_hat_23_substeps(self):
        key = next(self.space.keys())
        self.assertEqual(len(self.space.flat_ids(key)), 23)

    def test_identitaetsschluessel_ergibt_originalreihenfolge(self):
        key = plan_space.PlanKey(order=(0, 1, 2, 3), layouts=(0, 0, 0, 0))
        plan = plan_space.strip_null_subjects(plan_space.load_plan(TEST_PLAN))
        expected = tuple(s["id"] for b in plan["assembleSteps"] for s in b["subSteps"])
        self.assertEqual(self.space.flat_ids(key), expected)

    def test_plan_json_nummeriert_neu_und_laesst_ids_in_ruhe(self):
        key = plan_space.PlanKey(order=(3, 2, 1, 0), layouts=(0, 0, 0, 0))
        plan = self.space.plan_json(key)
        self.assertEqual([b["step"] for b in plan["assembleSteps"]], ["0", "1", "2", "3"])
        self.assertEqual([b["connection"] for b in plan["assembleSteps"]],
                         ["A0038208256_A4108858814", "A0038208256_A4108858714",
                          "A4108804072_A0005405654", "A4108804072_A4108851114"])
        for block in plan["assembleSteps"]:
            expected = [str(i + 1) for i in range(len(block["subSteps"]))]
            self.assertEqual([s["step"] for s in block["subSteps"]], expected)
        # IDs unveraendert
        all_ids = {s["id"] for b in plan["assembleSteps"] for s in b["subSteps"]}
        self.assertEqual(len(all_ids), 23)
        self.assertIn("de10e5ef4ebbb4dc2d60cdafe3eaead1c395e0fb84f4db1d02caba0ba81670e1", all_ids)

    def test_plan_json_behaelt_kopf_und_teilelisten(self):
        key = next(self.space.keys())
        plan = self.space.plan_json(key)
        self.assertEqual(plan["type"], "plan")
        self.assertEqual(plan["assembly"], "front_bumper")
        self.assertEqual(len(plan["components"]), 14)
        self.assertEqual(len(plan["parts"]), 39)


class PlanIdTest(unittest.TestCase):
    """Das id-Schema <stem>_plan_o<Reihenfolge>_l<Layouts>."""

    def setUp(self):
        self.space = plan_space.PlanSpace(
            plan_space.strip_null_subjects(plan_space.load_plan(TEST_PLAN)))

    def test_schema(self):
        key = plan_space.PlanKey(order=(2, 0, 3, 1), layouts=(0, 5, 0, 2))
        self.assertEqual(plan_space.format_plan_id("front_bumper", key),
                         "front_bumper_plan_o2-0-3-1_l0-5-0-2")

    def test_stamm_kommt_aus_assembly(self):
        self.assertEqual(self.space.id_stem, "front_bumper")
        self.assertEqual(plan_space.plan_id_stem({"assembly": "x"}), "x")
        self.assertEqual(plan_space.plan_id_stem({}), "plan")

    def test_id_kommt_aus_dem_schluessel_statt_aus_dem_eingabeplan(self):
        raw = plan_space.load_plan(TEST_PLAN)
        self.assertEqual(raw["id"], "front_bumper_plan_1")
        self.assertEqual(self.space.plan_json(self.space.initial_key())["id"],
                         "front_bumper_plan_o0-1-2-3_l0-0-0-0")
        key = plan_space.PlanKey(order=(3, 2, 1, 0), layouts=(0, 0, 0, 0))
        self.assertEqual(self.space.plan_json(key)["id"],
                         "front_bumper_plan_o3-2-1-0_l0-0-0-0")

    def test_key_from_id_ist_die_umkehrung(self):
        # Die Umkehrbarkeit schliesst die Eindeutigkeit mit ein: zwei Plaene
        # mit derselben id koennten nicht beide auf ihren Schluessel zurueck.
        for _, key in zip(range(500), self.space.keys()):
            self.assertEqual(plan_space.key_from_id(self.space.plan_id(key)), key)

    def test_key_from_id_meckert_bei_fremdem_schema(self):
        with self.assertRaises(ValueError):
            plan_space.key_from_id("front_bumper_plan_1")
        with self.assertRaises(ValueError):
            plan_space.key_from_id("front_bumper_plan_o0-1_l0-0-0")


class MatchingTest(unittest.TestCase):
    def setUp(self):
        self.space = plan_space.PlanSpace(
            plan_space.strip_null_subjects(plan_space.load_plan(TEST_PLAN)))
        self.identity = plan_space.PlanKey(order=(0, 1, 2, 3), layouts=(0, 0, 0, 0))

    def test_leeres_praefix_passt_auf_alles(self):
        self.assertEqual(self.space.count_matching(()), 138240)

    def test_vollstaendige_folge_passt_auf_genau_einen(self):
        flat = self.space.flat_ids(self.identity)
        self.assertEqual(self.space.count_matching(flat), 1)
        self.assertEqual(list(self.space.matching(flat)), [self.identity])

    def test_matches_stimmt_mit_count_ueberein(self):
        flat = self.space.flat_ids(self.identity)
        self.assertTrue(self.space.matches(self.identity, flat[:5]))
        other = plan_space.PlanKey(order=(1, 0, 2, 3), layouts=(0, 0, 0, 0))
        self.assertFalse(self.space.matches(other, flat[:5]))

    def test_blocksprung_passt_auf_keinen(self):
        # 2 Substeps aus Block 0, dann der erste Substep aus Block 2
        prefix = (
            "de10e5ef4ebbb4dc2d60cdafe3eaead1c395e0fb84f4db1d02caba0ba81670e1",
            "d711d88c0baf60d5d5fa1371ebd1f2c613e6bc2327af9e082d701e2cb4aaa0f5",
            "56bfba6811d059cd47028722f5dc9a043965373832c9587a3c09122cd3dcd62e",
        )
        self.assertEqual(self.space.count_matching(prefix), 0)
        self.assertEqual(list(self.space.matching(prefix)), [])
        self.assertIsNone(self.space.sample_matching(prefix, random.Random(1)))

    def test_unbekannte_id_passt_auf_keinen(self):
        self.assertEqual(self.space.count_matching(("x_666",)), 0)

    def test_doppelter_step_passt_auf_keinen(self):
        first = self.space.flat_ids(self.identity)[0]
        self.assertEqual(self.space.count_matching((first, first)), 0)

    def test_erster_substep_von_block0_grenzt_korrekt_ein(self):
        # Block 0 hat genau 1 Layout, seine 3 Substeps stehen fest.
        # Danach bleiben 3! Reihenfolgen der restlichen Bloecke * deren Layouts.
        prefix = ("de10e5ef4ebbb4dc2d60cdafe3eaead1c395e0fb84f4db1d02caba0ba81670e1",)
        self.assertEqual(self.space.count_matching(prefix), 6 * 576 * 1 * 10)

    def test_count_matching_stimmt_mit_matching_ueberein(self):
        flat = self.space.flat_ids(self.identity)
        for length in (0, 1, 3, 7, 12, 23):
            prefix = flat[:length]
            self.assertEqual(self.space.count_matching(prefix),
                             sum(1 for _ in self.space.matching(prefix)),
                             f"Laenge {length}")

    def test_sample_matching_liefert_passenden_schluessel(self):
        flat = self.space.flat_ids(self.identity)
        rng = random.Random(42)
        for _ in range(20):
            key = self.space.sample_matching(flat[:7], rng)
            self.assertIsNotNone(key)
            self.assertTrue(self.space.matches(key, flat[:7]))

    def test_sample_matching_ist_mit_seed_reproduzierbar(self):
        a = self.space.sample_matching((), random.Random(7))
        b = self.space.sample_matching((), random.Random(7))
        self.assertEqual(a, b)


class AblageTest(unittest.TestCase):
    def setUp(self):
        self.space = plan_space.PlanSpace(
            plan_space.strip_null_subjects(plan_space.load_plan(TEST_PLAN)))

    def test_write_index_und_read_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, index = plan_space.write_index(self.space, Path(tmp))
            self.assertTrue(base.exists())
            self.assertTrue(index.exists())
            lines = index.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 138240)
            loaded = list(plan_space.read_index(Path(tmp)))
            self.assertEqual(len(loaded), 138240)
            self.assertEqual(loaded[0], next(self.space.keys()))
            self.assertTrue(all(isinstance(k, plan_space.PlanKey) for k in loaded[:5]))

    def test_write_index_basis_ist_der_bereinigte_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, _ = plan_space.write_index(self.space, Path(tmp))
            reloaded = json.loads(base.read_text(encoding="utf-8"))
            self.assertEqual(reloaded["assembly"], "front_bumper")
            self.assertEqual(len(reloaded["assembleSteps"]), 4)

    def test_write_plans_schreibt_eine_zeile_je_plan(self):
        # Auf einem kuenstlich kleinen Raum, damit der Test schnell bleibt.
        small = plan_space.strip_null_subjects(plan_space.load_plan(TEST_PLAN))
        small["assembleSteps"] = small["assembleSteps"][:1]
        space = plan_space.PlanSpace(small)
        self.assertEqual(space.count(), 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = plan_space.write_plans(space, Path(tmp))
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            plan = json.loads(lines[0])
            self.assertEqual(plan["type"], "plan")
            self.assertEqual(len(plan["assembleSteps"]), 1)


class SzenarienTest(unittest.TestCase):
    def setUp(self):
        self.space = plan_space.PlanSpace(
            plan_space.strip_null_subjects(plan_space.load_plan(TEST_PLAN)))
        self.scenarios = {s["scenario"]: s
                          for s in plan_space.build_scenarios(self.space, "front_bumper")}

    def test_sieben_szenarien_mit_erwarteten_namen(self):
        self.assertEqual(sorted(self.scenarios), [
            "01_happy_original", "02_valid_regrouped", "03_valid_late_divergence",
            "04_block_jump", "05_unknown_action", "06_duplicate_step",
            "07_early_stop",
        ])

    def test_jedes_szenario_hat_die_pflichtfelder(self):
        for name, s in self.scenarios.items():
            for field in ("scenario", "description", "assemblyID", "expectation",
                          "terminal", "steps"):
                self.assertIn(field, s, name)
            self.assertEqual(s["assemblyID"], "front_bumper")
            self.assertTrue(s["steps"], name)
            for step in s["steps"]:
                self.assertIn("actionID", step)

    def test_terminal_feld_ist_maschinenlesbar_und_korrekt(self):
        # Finding 3: run_scenarios.py liest genau dieses Feld, nicht die
        # Prosa aus expectation.
        expected = {
            "01_happy_original": "planComplete",
            "02_valid_regrouped": "planComplete",
            "03_valid_late_divergence": "planComplete",
            "04_block_jump": "noMatchingPlan",
            "05_unknown_action": "noMatchingPlan",
            "06_duplicate_step": "noMatchingPlan",
            "07_early_stop": None,
        }
        for name, terminal in expected.items():
            self.assertEqual(self.scenarios[name]["terminal"], terminal, name)

    def _ids(self, name):
        return tuple(x["actionID"] for x in self.scenarios[name]["steps"])

    def test_01_ist_eine_vollstaendige_gueltige_folge(self):
        ids = self._ids("01_happy_original")
        self.assertEqual(len(ids), 23)
        self.assertEqual(self.space.count_matching(ids), 1)

    def test_02_ist_gueltig_und_verschieden_von_01(self):
        ids = self._ids("02_valid_regrouped")
        self.assertEqual(len(ids), 23)
        self.assertEqual(self.space.count_matching(ids), 1)
        self.assertNotEqual(ids, self._ids("01_happy_original"))

    def test_03_ist_gueltig_und_weicht_spaet_ab(self):
        ids = self._ids("03_valid_late_divergence")
        self.assertEqual(len(ids), 23)
        self.assertEqual(self.space.count_matching(ids), 1)
        original = self._ids("01_happy_original")
        common_len = 0
        while common_len < 23 and ids[common_len] == original[common_len]:
            common_len += 1
        self.assertGreaterEqual(common_len, 12)
        self.assertLess(common_len, 23)

    def test_04_bis_06_passen_auf_keinen_plan(self):
        for name in ("04_block_jump", "05_unknown_action", "06_duplicate_step"):
            self.assertEqual(self.space.count_matching(self._ids(name)), 0, name)

    def test_04_hat_ein_gueltiges_praefix_vor_dem_sprung(self):
        ids = self._ids("04_block_jump")
        self.assertGreater(self.space.count_matching(ids[:-1]), 0)

    def test_05_endet_auf_x_666(self):
        self.assertEqual(self._ids("05_unknown_action")[-1], "x_666")

    def test_06_wiederholt_den_letzten_step(self):
        ids = self._ids("06_duplicate_step")
        self.assertEqual(ids[-1], ids[-2])

    def test_07_ist_ein_gueltiges_unvollstaendiges_praefix(self):
        ids = self._ids("07_early_stop")
        self.assertLess(len(ids), 23)
        self.assertGreater(self.space.count_matching(ids), 0)

    def test_lesehilfe_felder_sind_vorhanden(self):
        step = self.scenarios["01_happy_original"]["steps"][0]
        self.assertIn("_connection", step)
        self.assertIn("_actionType", step)

    def test_write_scenarios_schreibt_sieben_dateien(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = plan_space.write_scenarios(self.space, "front_bumper", Path(tmp))
            self.assertEqual(len(paths), 7)
            for p in paths:
                loaded = json.loads(p.read_text(encoding="utf-8"))
                self.assertEqual(loaded["assemblyID"], "front_bumper")


if __name__ == "__main__":
    unittest.main()
