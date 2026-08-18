#!/usr/bin/env python3
"""Planraum eines Montageplans: Permutation, Ablage, Praefix-Matching.

Ein Plan wird nie als Ganzes vorgehalten, sondern durch einen Schluessel
(Reihenfolge der assembleStep-Bloecke, Layout-Nummer je Block) beschrieben und
bei Bedarf rekonstruiert. Bei 138240 Plaenen fuer den Testplan ist das der
Unterschied zwischen 5,8 MB und 1,7 GB.

Dieses Modul kennt kein MQTT und keine Dateien ausser dem Plan selbst.
"""

import itertools
import json
from collections import namedtuple
from itertools import groupby, permutations
from math import factorial
from pathlib import Path

EMPTY_SUBJECTS = (None, "", "none")

# Obergrenze fuer die Layoutzahl EINES Blocks (Finding 2 aus dem Review vom
# 2026-08-17). _expand() materialisiert das Kreuzprodukt der Permutationen -
# bei einer Verbindung mit 7 gleichartigen Elementen sind das schon 25 Mio.
# Layouts, 377s und 15 GB RAM. 2_000_000 liegt bewusst zwischen dem noch
# vertretbaren 6er-Fall (518400 Layouts, 5.5s, 270 MB) und dem 7er-Fall.
DEFAULT_LAYOUT_CEILING = 2_000_000

# Obergrenze fuer die GESAMTE Planzahl beim Ablegen (Finding 1). Der Reasoner
# selbst kommt mit beliebig grossen Planraeumen klar (count_matching und
# sample_matching enumerieren nie), aber write_index/write_plans iterieren
# jeden Schluessel einzeln - bei einem realen Plan mit vielen Bloecken kann
# das mehrere Groessenordnungen ueber der Layoutzahl eines einzelnen Blocks
# liegen (blocks! multipliziert durch). 10_000_000 ist der Vorschlag aus dem
# Review.
DEFAULT_STORE_CEILING = 10_000_000


def load_plan(path):
    """Liest einen Montageplan als dict."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def strip_null_subjects(plan):
    """Liefert eine Kopie des Plans ohne Substeps mit leerem subject.

    Das Original bleibt unveraendert. Betrifft Plaene wie
    OR_JSON_FrontBumper_Plan_1 (14 solche Substeps); der Testplan hat keine.
    """
    steps = []
    for block in plan["assembleSteps"]:
        kept = [s for s in block["subSteps"] if s.get("subject") not in EMPTY_SUBJECTS]
        steps.append({**block, "subSteps": kept})
    return {**plan, "assembleSteps": steps}


def _runs(substeps):
    """Layout A: maximale Laeufe gleichen actionType, jeder Lauf bleibt an Ort."""
    return [list(g) for _, g in groupby(substeps, key=lambda s: s["actionType"])]


def _grouped(substeps):
    """Layout B: nach actionType gruppiert, Gruppenreihenfolge = erstes Auftreten."""
    order, buckets = [], {}
    for step in substeps:
        kind = step["actionType"]
        if kind not in buckets:
            buckets[kind] = []
            order.append(kind)
        buckets[kind].append(step)
    return [buckets[kind] for kind in order]


def _expand(blocks):
    """Kreuzprodukt der Permutationen innerhalb jedes Blocks."""
    out = [[]]
    for block in blocks:
        out = [acc + list(perm) for acc in out for perm in permutations(block)]
    return out


def _layout_bound(substeps):
    """Obere Schranke der Layoutzahl eines Blocks, OHNE zu expandieren.

    Layout A liefert das Produkt der Fakultaeten der Lauflaengen, Layout B das
    Produkt der Fakultaeten der Gruppengroessen (Finding 2). Die tatsaechliche
    Anzahl nach dedup(A + B) in layouts_for() ist hoechstens die Summe beider -
    das reicht als billiger Vorab-Check, bevor _expand() etwas alloziert.
    """
    a = 1
    for run in _runs(substeps):
        a *= factorial(len(run))
    b = 1
    for group in _grouped(substeps):
        b *= factorial(len(group))
    return a + b


def layouts_for(substeps):
    """Alle zulaessigen Anordnungen einer Substep-Folge.

    Vereinigung aus Layout A (nur benachbarte Gleichtypen tauschen) und
    Layout B (nach Typ gruppieren, dann innerhalb der Gruppen tauschen),
    dedupliziert ueber die ID-Folge. Die Originalreihenfolge steht immer vorn.
    """
    seen = {}
    for layout in _expand(_runs(substeps)) + _expand(_grouped(substeps)):
        seen.setdefault(tuple(s["id"] for s in layout), layout)
    return list(seen.values())


def _check_turnon_before_tighten(layout, connection):
    """Prueft, dass in `layout` jedes tighten hinter dem turnon mit gleichem
    subject steht (Spec 3.2). Fuer test_plan_1 3.json ist das nachgerechnet
    und als Test festgehalten; hier wird es zur Konstruktionszeit erzwungen
    (Finding 6), damit ein ueber --plan eingespeister Plan mit umgekehrter
    Typreihenfolge nicht still physikalisch unmoegliche Reihenfolgen erzeugt.

    Wirft ValueError statt False zurueckzugeben, weil der Aufrufer (PlanSpace)
    an dieser Stelle sowieso nur abbrechen kann - es gibt keine sinnvolle
    Fortsetzung fuer einen Plan, der die Grundannahme verletzt.
    """
    pos = {}
    for i, s in enumerate(layout):
        pos.setdefault((s["actionType"], s["subject"]), i)
    for s in layout:
        if s["actionType"] != "tighten":
            continue
        turnon = pos.get(("turnon", s["subject"]))
        tighten = pos[("tighten", s["subject"])]
        if turnon is not None and turnon >= tighten:
            raise ValueError(
                f"Block {connection}: tighten fuer subject {s['subject']!r} "
                f"steht nicht hinter dem zugehoerigen turnon - Eingabeplan "
                f"verletzt die in Spec 3.2 vorausgesetzte Reihenfolge "
                f"turnon -> tighten")


PlanKey = namedtuple("PlanKey", "order layouts")


class PlanSpace:
    """Der Raum aller zulaessigen Ausfuehrungsreihenfolgen eines Plans.

    Ein Plan ist eine Permutation der assembleStep-Bloecke plus je Block eine
    Layout-Nummer. Die Bloecke bleiben dabei geschlossen - kein Plan verschraenkt
    Substeps zweier assembleSteps.
    """

    def __init__(self, plan, layout_ceiling=DEFAULT_LAYOUT_CEILING):
        """Baut den Planraum.

        `layout_ceiling` ist die Obergrenze fuer die geschaetzte Layoutzahl
        JE Block (Finding 2) - hier bewusst als Konstruktorparameter, damit
        ein Aufrufer sie fuer einen konkreten Plan gezielt anheben kann. Vor
        jeder Expansion wird zusaetzlich Spec 3.2 (turnon vor tighten)
        geprueft (Finding 6).
        """
        self.plan = plan
        self.blocks = plan["assembleSteps"]
        self.layouts = []
        for block in self.blocks:
            substeps = block["subSteps"]
            bound = _layout_bound(substeps)
            if bound > layout_ceiling:
                raise ValueError(
                    f"Block {block['connection']}: geschaetzte Layoutzahl "
                    f"{bound} liegt ueber der Obergrenze {layout_ceiling} - "
                    f"_expand() wuerde das materialisieren muessen. "
                    f"layout_ceiling explizit anheben, wenn das wirklich "
                    f"gewollt ist.")
            layouts = layouts_for(substeps)
            for layout in layouts:
                _check_turnon_before_tighten(layout, block["connection"])
            self.layouts.append(layouts)

    def count(self):
        n = factorial(len(self.blocks))
        for lay in self.layouts:
            n *= len(lay)
        return n

    def keys(self):
        """Iteriert alle Planschluessel. Reihenfolge ist stabil, aber unspezifiziert."""
        n = len(self.blocks)
        for order in itertools.permutations(range(n)):
            ranges = [range(len(self.layouts[b])) for b in order]
            for combo in itertools.product(*ranges):
                yield PlanKey(order=order, layouts=combo)

    def _block_steps(self, block_index, layout_index):
        return self.layouts[block_index][layout_index]

    def flat_ids(self, key):
        """Die Substep-IDs in Ausfuehrungsreihenfolge, ueber alle Bloecke hinweg."""
        return tuple(
            s["id"]
            for pos, block_index in enumerate(key.order)
            for s in self._block_steps(block_index, key.layouts[pos])
        )

    def plan_json(self, key):
        """Baut den vollstaendigen Plan zu einem Schluessel.

        step-Felder werden neu vergeben (assembleSteps ab "0", subSteps je Block
        ab "1"); id-Felder bleiben unberuehrt, sie sind Inhaltshashes.
        """
        steps = []
        for pos, block_index in enumerate(key.order):
            block = self.blocks[block_index]
            substeps = [
                {**s, "step": str(i + 1)}
                for i, s in enumerate(self._block_steps(block_index, key.layouts[pos]))
            ]
            steps.append({**block, "step": str(pos), "subSteps": substeps})
        return {**self.plan, "assembleSteps": steps}

    def _free_count(self, remaining):
        """Anzahl Kombinationen, wenn die Bloecke in `remaining` voellig frei sind."""
        n = factorial(len(remaining))
        for b in remaining:
            n *= len(self.layouts[b])
        return n

    def _walk(self, prefix, remaining, emit):
        """Besucht alle passenden Fortsetzungen.

        `emit(prefix_blocks, remaining)` wird fuer jeden Zweig aufgerufen, in dem
        das Praefix aufgebraucht ist. `prefix_blocks` ist die Liste der bereits
        festgelegten (Blockindex, Layoutindex)-Paare.
        """
        yield from self._walk_inner(prefix, tuple(remaining), (), emit)

    def _walk_inner(self, prefix, remaining, fixed, emit):
        if not prefix:
            yield from emit(fixed, remaining)
            return
        for block_index in remaining:
            rest = tuple(b for b in remaining if b != block_index)
            for layout_index, layout in enumerate(self.layouts[block_index]):
                ids = [s["id"] for s in layout]
                if len(ids) <= len(prefix):
                    if ids != list(prefix[:len(ids)]):
                        continue
                    yield from self._walk_inner(
                        prefix[len(ids):], rest, fixed + ((block_index, layout_index),),
                        emit)
                else:
                    if ids[:len(prefix)] != list(prefix):
                        continue
                    # Block ist angefangen, aber nicht fertig: der Rest ist frei.
                    yield from emit(fixed + ((block_index, layout_index),), rest)

    def matching(self, prefix):
        """Iteriert alle Planschluessel, deren flache Folge mit `prefix` beginnt."""
        def emit(fixed, remaining):
            for tail in itertools.permutations(remaining):
                ranges = [range(len(self.layouts[b])) for b in tail]
                for combo in itertools.product(*ranges):
                    yield PlanKey(
                        order=tuple(b for b, _ in fixed) + tail,
                        layouts=tuple(l for _, l in fixed) + combo,
                    )

        yield from self._walk(tuple(prefix), range(len(self.blocks)), emit)

    def count_matching(self, prefix):
        """Zaehlt die passenden Plaene, ohne sie aufzuzaehlen."""
        def emit(fixed, remaining):
            yield self._free_count(remaining)

        return sum(self._walk(tuple(prefix), range(len(self.blocks)), emit))

    def matches(self, key, prefix):
        """Beginnt die flache Folge von `key` mit `prefix`?"""
        prefix = tuple(prefix)
        return self.flat_ids(key)[:len(prefix)] == prefix

    def sample_matching(self, prefix, rng):
        """Zieht gleichverteilt einen passenden Plan, oder None wenn keiner passt.

        Zaehlt zuerst die Treffer je Zweig und waehlt dann gewichtet - so muss
        nicht die gesamte Trefferliste materialisiert werden.
        """
        def emit(fixed, remaining):
            yield (fixed, remaining, self._free_count(remaining))

        branches = list(self._walk(tuple(prefix), range(len(self.blocks)), emit))
        total = sum(w for _, _, w in branches)
        if total == 0:
            return None
        target = rng.randrange(total)
        for fixed, remaining, weight in branches:
            if target < weight:
                tail = list(remaining)
                rng.shuffle(tail)
                combo = tuple(rng.randrange(len(self.layouts[b])) for b in tail)
                return PlanKey(
                    order=tuple(b for b, _ in fixed) + tuple(tail),
                    layouts=tuple(l for _, l in fixed) + combo,
                )
            target -= weight
        raise AssertionError("unerreichbar")


BASE_NAME = "plan_base.json"
INDEX_NAME = "plan_index.jsonl"
PLANS_NAME = "plans.jsonl"


def write_index(space, out_dir):
    """Legt den bereinigten Plan plus einen Schluessel je Zeile ab (~5,8 MB)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / BASE_NAME
    base.write_text(json.dumps(space.plan, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
    index = out_dir / INDEX_NAME
    with index.open("w", encoding="utf-8", newline="\n") as fh:
        for key in space.keys():
            fh.write(json.dumps({"order": list(key.order),
                                 "layouts": list(key.layouts)},
                                separators=(",", ":")) + "\n")
    return base, index


def write_plans(space, out_dir):
    """Legt jeden Plan vollstaendig ab, eine Zeile je Plan (~705 MB beim Testplan)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / PLANS_NAME
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for key in space.keys():
            fh.write(json.dumps(space.plan_json(key), ensure_ascii=False,
                                separators=(",", ":")) + "\n")
    return path


def read_index(out_dir):
    """Liest die Schluessel aus plan_index.jsonl zurueck."""
    path = Path(out_dir) / INDEX_NAME
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            yield PlanKey(order=tuple(raw["order"]), layouts=tuple(raw["layouts"]))


UNKNOWN_ACTION_ID = "x_666"


def _step_entry(space, action_id):
    """Baut einen Szenario-Eintrag mit Lesehilfe-Feldern."""
    for block in space.blocks:
        for s in block["subSteps"]:
            if s["id"] == action_id:
                return {"actionID": action_id,
                        "_connection": block["connection"],
                        "_actionType": s["actionType"]}
    return {"actionID": action_id, "_connection": "none", "_actionType": "unknown"}


TERMINAL_TYPES = ("planComplete", "noMatchingPlan")


def _scenario(space, name, description, expectation, terminal, assembly_id, ids):
    """Baut ein Szenario-dict.

    `expectation` ist die deutsche Prosaerklaerung fuer Menschen (Spec 8.1,
    vom Client geloggt). `terminal` ist das maschinenlesbare Gegenstueck, das
    run_scenarios.py tatsaechlich prueft (Finding 3): "planComplete",
    "noMatchingPlan" oder None fuer 07_early_stop, das keine Terminalnachricht
    hat.
    """
    assert terminal is None or terminal in TERMINAL_TYPES, terminal
    return {
        "scenario": name,
        "description": description,
        "assemblyID": assembly_id,
        "expectation": expectation,
        "terminal": terminal,
        "steps": [_step_entry(space, i) for i in ids],
    }


def _last_layout_index(space, block_index):
    return len(space.layouts[block_index]) - 1


def build_scenarios(space, assembly_id):
    """Die sieben Szenarien aus Spec 8.2 als dicts."""
    n = len(space.blocks)
    identity = PlanKey(order=tuple(range(n)), layouts=(0,) * n)
    original = space.flat_ids(identity)

    # 02: Bloecke rueckwaerts, jeder Block im letzten Layout (Layout B ist
    # per Konstruktion hinten in layouts_for, siehe Task 2).
    reversed_order = tuple(reversed(range(n)))
    regrouped_key = PlanKey(
        order=reversed_order,
        layouts=tuple(_last_layout_index(space, b) for b in reversed_order))
    regrouped = space.flat_ids(regrouped_key)

    # 03: spaetestmoegliche Abweichung von `original` auf eine andere gueltige Folge.
    # Sucht von hinten und bricht beim ersten Treffer ab, findet also die spaeteste
    # Abweichung. `space.matching(original[:k])` liefert fuer grosse `k` nur wenige
    # Kandidaten, der Aufruf ist also billig.
    late = original
    for k in range(len(original) - 1, 0, -1):
        candidate = next(
            (space.flat_ids(key) for key in space.matching(original[:k])
             if space.flat_ids(key)[k] != original[k]),
            None)
        if candidate is not None:
            late = candidate
            break

    # 04: 2 Substeps aus dem ersten Block, dann in einen anderen Block springen
    first_block = space.blocks[identity.order[0]]
    other_block = next(b for b in space.blocks if b is not first_block)
    jump = (tuple(s["id"] for s in space.layouts[0][0][:2])
            + (other_block["subSteps"][0]["id"],))

    return [
        _scenario(space, "01_happy_original",
                  "Originalreihenfolge der Bloecke, Originallayout je Block",
                  "planComplete", "planComplete", assembly_id, original),
        _scenario(space, "02_valid_regrouped",
                  "Bloecke rueckwaerts, jeder Block nach actionType gruppiert",
                  "mindestens ein Planwechsel, dann planComplete",
                  "planComplete", assembly_id, regrouped),
        _scenario(space, "03_valid_late_divergence",
                  "Gueltige Folge, die erst spaet von 01 abweicht",
                  "spaeter Planwechsel, dann planComplete",
                  "planComplete", assembly_id, late),
        _scenario(space, "04_block_jump",
                  "Verlaesst den ersten Block nach 2 Substeps und springt in einen anderen",
                  "noMatchingPlan", "noMatchingPlan", assembly_id, jump),
        _scenario(space, "05_unknown_action",
                  "Gueltiges Praefix, dann eine actionID, die es nicht gibt",
                  "noMatchingPlan", "noMatchingPlan",
                  assembly_id, original[:3] + (UNKNOWN_ACTION_ID,)),
        _scenario(space, "06_duplicate_step",
                  "Gueltiges Praefix, letzter Step doppelt gemeldet",
                  "noMatchingPlan", "noMatchingPlan",
                  assembly_id, original[:4] + (original[3],)),
        _scenario(space, "07_early_stop",
                  "Gueltige Folge, nach der Haelfte abgebrochen",
                  "Reasoner wartet still weiter",
                  None, assembly_id, original[:len(original) // 2]),
    ]


def write_scenarios(space, assembly_id, out_dir):
    """Schreibt die sieben Szenariodateien und liefert ihre Pfade."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for scenario in build_scenarios(space, assembly_id):
        path = out_dir / f"{scenario['scenario']}.json"
        path.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")
        paths.append(path)
    return paths
