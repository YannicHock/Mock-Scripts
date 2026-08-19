# Implementation

Zwei über MQTT gekoppelte Mock-Skripte für die Montageplanung, plus eine Änderung
am bestehenden Konverter `converter/resultat_to_json.py`.

Ausführliche Begründung der Entwurfsentscheidungen:
[`docs/superpowers/specs/2026-08-17-mqtt-mocks-design.md`](docs/superpowers/specs/2026-08-17-mqtt-mocks-design.md).

---

## 1 Grundsätzliches Design

### 1.1 Was die Mocks tun

Ein ASP-Solver liefert Montagepläne: eine Liste von `assembleSteps` (je einer pro
Verbindung zwischen Bauteilen), jeder mit `subSteps` (`position`, `insert`,
`turnon`, `tighten`, …). Die Frage, um die sich alles dreht: **Welche anderen
Ausführungsreihenfolgen sind gleichwertig?** Ein Werker darf vier Muttern in
beliebiger Reihenfolge festziehen, oder erst alle Schrauben einsetzen und dann
alle festziehen.

- **Operation Reasoner** (`mocks/or_reasoner_mock.py`) spannt diesen Raum auf, schickt
  einen Plan über MQTT und grenzt den Raum mit jedem gemeldeten Arbeitsschritt ein.
- **Assembly Client** (`mocks/assembly_client_mock.py`) spielt eine feste Schrittfolge
  aus einer Szenariodatei ab und meldet jeden Schritt zurück.

Beide sind **Ausgabe-Mocks**. Sie simulieren kein echtes Reasoning und keine echte
Aktionserkennung. Der Wert liegt im Nachrichtenkontrakt und im Verhalten an den
Grenzen.

### 1.2 Die Permutationsregel

Für die Substep-Folge eines assembleSteps:

- **Layout A** — zerlege in maximale Läufe gleichen `actionType`, permutiere
  innerhalb jedes Laufs, die Läufe bleiben an ihrem Platz.
- **Layout B** — gruppiere alle Substeps nach `actionType` (Gruppenreihenfolge =
  Reihenfolge des ersten Auftretens), permutiere innerhalb jeder Gruppe.

Gültig ist `dedup(A ∪ B)`. Ein Plan ist eine beliebige Permutation der
assembleStep-Blöcke, kombiniert mit je einem Layout pro Block.

**Die Blöcke bleiben geschlossen.** Kein Plan verschränkt Substeps zweier
assembleSteps. Wer einen begonnenen assembleStep verlässt und später zurückkehrt,
trifft auf keinen Plan — das ist die Eigenschaft, auf der das gesamte Matching ruht.

Dass die assembleSteps frei permutierbar sind, ist kein Zusatz: sie haben alle
`actionType: "assemble"` und bilden damit einen einzigen Lauf gleichen Typs.

Für den mitgelieferten Eingabeplan `data/plans/test_plan_1_3.json`:

| connection | Typ-Folge | Layouts |
|---|---|---|
| `A4108804072_A4108851114` | posi, inse, tigh | 1 |
| `A4108804072_A0005405654` | posi, turn×4, tigh×4 | 576 |
| `A0038208256_A4108858714` | posi, inse, turn, tigh | 1 |
| `A0038208256_A4108858814` | posi, inse, turn, inse, turn, tigh, tigh | 10 |

`4! × 576 × 10 = ` **138 240 Pläne**, alle mit paarweise verschiedener flacher
ID-Folge.

### 1.3 Der Kerngedanke: ein Plan wird nie ganz gehalten

138 240 vollständige Pläne wären 1,7 GB. Stattdessen benennt ein **Planschlüssel**
`PlanKey(order, layouts)` einen Plan durch wenige kleine Zahlen: `order[i]` ist der
Index des assembleSteps an Position `i`, `layouts[i]` dessen Layout-Nummer.
`plan_json(key)` baut den vollständigen Plan bei Bedarf in Millisekunden.

Ebenso das Matching: `matching(prefix)` läuft **nicht** linear über alle Pläne.
Weil die Blöcke geschlossen bleiben, zerfällt jedes Präfix in „diese Blöcke sind
fertig" plus „in diesem Block sind k Substeps erledigt", und ganze Zweige fallen
auf einmal weg. Gemessen: 24 Präfixlängen in 0,031 s.

`sample_matching` zieht **gleichverteilt** aus der Treffermenge, ohne sie zu
materialisieren — es gewichtet Zweige nach ihrer Trefferzahl und steigt dann ab.

### 1.4 Die `id` des gesendeten Plans

Der Eingabeplan trägt eine feste `id` (`front_bumper_plan_1`). Würde sie
unverändert mitgesendet, hießen alle 138 240 Pläne gleich — auf der Client-Seite
wäre ein Planwechsel an der Nachricht nicht zu erkennen. Deshalb bildet
`plan_json(key)` die `id` **aus dem Planschlüssel**:

```
front_bumper_plan_o2-0-3-1_l0-0-7-483
└────┬─────┘       └──┬──┘  └───┬───┘
  Stamm (= assembly)  │         │
                      │         └─ Layout-Nummer je Position, in derselben
                      │            Reihenfolge wie o — die 7 gehört zu
                      │            Block 3, nicht zu Block 2
                      └─ Reihenfolge der assembleStep-Blöcke
                         (Index im Originalplan)
```

Der Stamm ist `assembly` — dasselbe Feld, über das der Client den Plan ohnehin
adressiert (`assemblyID`).

Drei Eigenschaften, auf die es ankommt:

- **Sie wechselt genau dann, wenn der Plan wechselt.** Sie hängt am Schlüssel,
  nicht am Eingabeplan.
- **Sie ist deterministisch.** Derselbe Plan trägt in jedem Lauf und auf jeder
  Maschine dieselbe `id`, unabhängig von `--seed` und davon, als wievielter Plan
  er gesendet wurde.
- **Sie ist umkehrbar.** `PlanSpace.key_from_id(id)` liefert den Schlüssel
  zurück, `plan_json` daraus wieder den exakten Plan — eine `id` aus einem Log
  genügt, um den Plan zu rekonstruieren. Die Methode prüft dabei, ob der
  Schlüssel in *diesen* Planraum passt; die reine Formprüfung ist
  `plan_space.parse_plan_id`. Ihr Docstring nennt die Einschränkung, auf die es
  ankommt: Layout-Nummern gelten nur gegenüber demselben Eingabeplan — anders
  als die Step-`id`s, die Inhaltshashes sind.

Der initiale Plan heißt damit immer `front_bumper_plan_o0-1-2-3_l0-0-0-0`.

Beide Mocks loggen die `id`: der Reasoner bei Wahl und Wechsel, der Client jedes
Mal, wenn sich die empfangene `id` ändert. Sie ersetzt dort die frühere Ausgabe
„Blöcke …, Layouts …" — sie enthält dieselben Zahlen, nur in einer Form, die auf
beiden Seiten dieselbe ist und sich greppen lässt.

**Die `id`-Felder der Steps und Substeps bleiben unberührt** — das sind
Inhaltshashes über den ASP-Rohfakt (siehe 1.8), und der Client meldet Schritte
über sie.

### 1.5 Dateien

```
mocks/
  plan_space.py           Permutation, Matching, Ablage, Szenarien   (kein MQTT)
  mqtt_envelope.py        Nachrichtenformat, Brokerverbindung, Topics
  or_reasoner_mock.py     Mock 1: Zustandsmaschine + MQTT-Verdrahtung + CLI
  assembly_client_mock.py Mock 2: Replayer + MQTT-Verdrahtung + CLI
converter/
  resultat_to_json.py     bestehender Konverter (geändert, siehe 1.8)
scripts/
  run_scenarios.py        End-to-End-Lauf aller Szenarien
data/
  input/                  Solver-Ausgabe und ASP-Annotationen
  plans/                  Eingabeplan der Mocks
  reference/              Formatreferenzen: zwei Plan-Formen, zwei MQTT-Nachrichten
  output/                 erzeugte Konverter-Ausgabe (Golden File)
docker/mosquitto/         Broker
docs/                     Spec und Implementierungsplan
tests/                    unittest-Suite
generated/, scenarios/    Erzeugnisse zur Laufzeit, git-ignoriert
```

Die Wurzel enthält damit nur noch Konfiguration und die beiden Markdown-Dateien.
`mocks` und `converter` sind Pakete, also gilt `python -m mocks.or_reasoner_mock`
statt eines Skriptaufrufs; die Arbeitsverzeichnis-relativen Datenpfade setzen
voraus, dass du **aus der Projektwurzel** startest.

Der Schnitt folgt der Testbarkeit: `plan_space.py` trägt die gesamte Logik, die
etwas falsch machen kann, und braucht dafür weder Broker noch Netzwerk. Die beiden
Mocks sind dünn. Ebenso ist die Klasse `Reasoner` eine reine Zustandsmaschine ohne
MQTT-Kenntnis — deshalb sind alle Endzustände ohne Broker getestet.

### 1.6 MQTT-Kontrakt

| Topic | Richtung |
|---|---|
| `plan` | Reasoner → Client |
| `action` | Client → Reasoner |

QoS 1, **kein Retain**. Der Plan reist als **JSON-String** im Feld `data.plan`,
nicht als eingebettetes Objekt — das ist Absicht und kommt aus den Referenzdateien.

Terminalnachrichten laufen über dasselbe Topic `plan` und sind an `data.type`
erkennbar: `planComplete` oder `noMatchingPlan`.

> **Der Client muss vor dem Reasoner starten.** Der Reasoner sendet seine erste
> Plan-Nachricht unaufgefordert nach dem Verbinden. Ohne Retain verwirft der Broker
> sie, wenn noch niemand `plan` abonniert hat — beide Seiten warten dann endlos.
> `run_scenarios.py` hält diese Reihenfolge selbst ein.

Retain wäre die naheliegende Alternative und ist bewusst nicht gewählt: eine
retained Plan-Nachricht überlebt den Prozess und würde den nächsten Testlauf mit
einem Plan aus dem vorigen beginnen.

### 1.7 Zustandsmaschine des Reasoners

| Ereignis | Reaktion |
|---|---|
| Start | Planraum erzeugen und ablegen, zufälligen Plan senden (bei `--seed 0` den initialen) |
| Step passt, aktueller Plan passt noch | denselben Plan erneut senden |
| Step passt, aktueller Plan passt nicht mehr | zufällig einen der übrigen passenden senden |
| Kein Plan passt mehr | `noMatchingPlan` senden, beenden |
| Unbekannte `actionID` | wie „kein Plan passt" |
| Derselbe Step doppelt | keine Sonderbehandlung — fällt durchs Matching, endet ebenso |
| Alle Substeps abgearbeitet | `planComplete` senden, beenden |

### 1.8 Änderung am Konverter

Schraublöcher bekommen symbolische IDs statt roher Koordinaten:

```
vorher   "aspID": "-2.4200000762939453 , -1.1419999599456787 , 0.02800000086426735"
nachher  "aspID": "A0038208256_A4108858714_screwhole_1"
```

Regel: `<connection>_screwhole_<n>`, `n` pro Verbindung ab 1 in der Reihenfolge der
`has_connection_point`-Indizes. Gegen `data/input/Stossecke_Li_Annotationen_1.lp` geprüft:
reproduziert alle Schraubloch-IDs des Zielformats, und keine Koordinate kommt in
mehr als einer Verbindung vor — die Regel ist kollisionsfrei.

Das `position`-Feld **behält die Koordinate** (`"POS: <x , y , z>"`). Damit weicht
der Generator bewusst vom Zielplan ab, wo dort die symbolische ID wiederholt wird;
sonst ginge die Geometrie vollständig verloren und das Feld wäre eine Kopie von
`aspID`.

**Die `id`-Felder ändern sich dadurch nicht.** Sie sind SHA-256 über den
ASP-Rohfakt, der weiterhin Koordinaten enthält. Nachgemessen bei der Neuerzeugung
von `Resultat_output_2.json`: 17 `aspID`- und 38 `subject`-Werte geändert, **null**
`id`-Felder.

---

## 2 Getroffene Annahmen

Diese Punkte waren nicht vorgegeben und wurden im Verlauf entschieden. Wer etwas
davon anders braucht, findet hier die Stelle zum Ansetzen.

### 2.1 Zum Protokoll

1. **Lockstep.** Der Client wartet auf eine Plan-Nachricht, bevor er den nächsten
   Step sendet. Abgeleitet aus der Formulierung „der Client *antwortet* wieder mit
   einem Step". Ein freilaufender Client in festem Takt wäre die Alternative.
2. **Terminalnachrichten sind eine Ergänzung.** Vorgegeben war „Logmeldung,
   beenden" — ohne gesendete Nachricht hätte der Client im Lockstep endlos
   gewartet. Deshalb sendet der Reasoner in beiden Endzuständen, bevor er sich
   beendet.
3. **Envelope-Kopffelder** stammen unverändert aus den Beispieldateien:
   `DFKI`/id 123 für den Reasoner, `ifak`/id 123 für den Client.
4. **Kein Leerlauf-Timeout.** Meldet der Client nichts mehr (Szenario 07), wartet
   der Reasoner, bis er von Hand beendet wird.
5. **Startreihenfolge Client vor Reasoner**, statt Retain zu verwenden — siehe 1.6.

### 2.2 Zur Permutation

6. **Variante A ∪ B.** Die Vorschrift ließ zwei Lesarten zu: nur benachbarte
   Gleichtypen tauschen (A), oder nach Typ gruppieren und dann permutieren (B).
   Beide werden erzeugt und dedupliziert. Nur A wären 12 Pläne, nur B 48, die
   Vereinigung ergibt 60 — beim endgültigen Eingabeplan 138 240.
7. **Physisch unmögliche Pläne entstehen nicht**, solange im Eingabeplan der erste
   `turnon` vor dem ersten `tighten` steht. Das wird beim Aufbau des Planraums für
   **jedes** erzeugte Layout geprüft und andernfalls mit Nennung des Blocks
   abgelehnt.
8. **`x_666`** ist die festgelegte `actionID` für einen nicht existierenden Step.

### 2.3 Zur Ablage

9. **Index als Default.** „Alle Pläne auf Platte" wird als verlustfreie
   Beschreibung erfüllt (`plan_base.json` + ein Schlüssel pro Zeile, ~5,8 MB) statt
   als 1,7 GB ausgeschriebener Pläne. `--store plans` schreibt sie voll aus.
    Wie der Index genau funktioniert und warum, ist in
    [`docs/index_explanation.md`](docs/index_explanation.md) ausfuehrlich
    beschrieben - als Gespraechsleitfaden, mit den gemessenen Zahlen und den
    absehbaren Rueckfragen.
10. **Obergrenzen statt unbegrenztem Schreiben.** Oberhalb von 10 000 000 Plänen
    verweigert die Ablage mit klarer Meldung; oberhalb von 2 000 000 Layouts pro
    Block verweigert der Aufbau, **bevor** etwas alloziert wird. Beides ist über
    `--max-store-count` bzw. im Code anhebbar. Ohne diese Grenzen hätte
    `Resultat_output_2.json` als Eingabe wortlos rund sechzehn Exabyte zu schreiben
    versucht (siehe 4).

### 2.4 Zu Sprache und Form

11. **Kommentare und Docstrings deutsch, Bezeichner englisch** — die Konvention des
    bestehenden `resultat_to_json.py`.
12. **Testmethodennamen bleiben deutsch.** Sie beschreiben das geprüfte Verhalten
    und sind keine Bezeichner im Sinne von Punkt 11.
13. **Quelltext ist ASCII-only**, Umlaute ausgeschrieben — ebenfalls die
    bestehende Konvention.

### 2.5 Offene Datenfehler im Eingabeplan

Gemeldet, aber **nicht behoben** — der Reasoner permutiert nur und prüft keine
Teilekonsistenz:

- `N910112006001_3` steht **zweimal** in `parts`.
- Dieselbe Instanz wird von zwei assembleSteps als `object` benutzt: an
  `A4108804072_A0005405654_screwhole_1` und an `A0038208256_A4108858814_screwhole_2`.
  Vermutlich sollte eine davon `_7` heißen.

Ebenfalls zur Kenntnis: `test_plan_1 3.json` enthält **keinen** Substep mit
`subject == null`. Die entsprechende Filterregel ist implementiert und getestet,
läuft mit diesem Plan aber ins Leere.

---

## 3 Usage Guide

### 3.1 Einrichten

```powershell
uv sync                                                   # Abhaengigkeiten
docker compose -f docker/mosquitto/compose.yaml up -d      # Broker starten
```

Der Broker hört auf `127.0.0.1:1883`, anonym, ohne Persistenz, ohne Restart-Policy
— nach einem Neustart des Rechners musst du ihn von Hand wieder starten.

```powershell
docker compose -f docker/mosquitto/compose.yaml down       # stoppen
docker logs -f mqtt-mock-broker                            # mitlesen
```

### 3.2 Der schnellste Weg: alle sieben Szenarien

```powershell
uv run python -m mocks.or_reasoner_mock --emit-scenarios scenarios   # einmalig
uv run python scripts/run_scenarios.py
```

Erwartet: `7/7 Szenarien wie erwartet`. Dauert einige Minuten — Szenario 07 wartet
allein 25 Sekunden, um zu belegen, dass beide Prozesse am Leben bleiben.

Der Lauf ist als **Demo** gedacht und gibt den vollstaendigen Dialog beider
Prozesse live aus: pro Szenario zuerst dessen Beschreibung und die Schrittfolge
nach assembleStep gruppiert, dann die verschraenkte Konversation, dann das
Ergebnis.

Der Reasoner legt dabei seine Entscheidung offen. Pro gemeldetem Schritt steht,
wie viele Plaene noch passen **und** ob er den aktuellen Plan behaelt oder
wechselt:

```
Step  3 c247185f3e84   13824 von 138240 Plaene passen  Plan 3 passt weiter
Step  6 404b512e0373    3456 von 138240 Plaene passen  Plan 3 passt nicht mehr
                                                       -> Plan 4: front_bumper_plan_o3-1-2-0_l9-26-0-0
```

Die Plaene sind fortlaufend nummeriert: fuer die Demo zaehlt nicht, *welcher*
Plan es ist, sondern ob es derselbe wie eben ist. Beides sind verschiedene
Groessen - ein Schritt kann den Raum einengen, ohne den Plan zu entwerten, und
umgekehrt.

Die Ergebniszeile fasst drei Zahlen zusammen: Schritte, Entwicklung des
Planraums (`138240 -> 1` beim Durchlauf, `138240 -> 0` beim Abbruch) und die
Zahl der Planwechsel - genau die Groesse, die Szenario 02 in seiner Erwartung
nennt.

```powershell
uv run python scripts/run_scenarios.py --quiet    # nur die Ergebniszeilen, fuer CI
```

### 3.3 Ein Szenario von Hand fahren

**Client zuerst**, sonst geht die erste Plan-Nachricht verloren (siehe 1.6).

Terminal 1:
```powershell
uv run python -m mocks.assembly_client_mock --scenario scenarios/01_happy_original.json
```

Sobald dort `warte auf den ersten Plan` steht, Terminal 2:
```powershell
uv run python -m mocks.or_reasoner_mock --seed 1
```

### 3.4 Die Szenarien

| Datei | Was sie prüft | Erwartetes Ergebnis |
|---|---|---|
| `01_happy_original` | Originalreihenfolge | `planComplete` |
| `02_valid_regrouped` | Blöcke umgekehrt, nach Typ gruppiert | Planwechsel, dann `planComplete` |
| `03_valid_late_divergence` | Abweichung erst bei Position 21 von 23 | später Planwechsel, dann `planComplete` |
| `04_block_jump` | verlässt einen assembleStep nach 2 Substeps | `noMatchingPlan` nach 3 Steps |
| `05_unknown_action` | meldet `x_666` | `noMatchingPlan` nach 4 Steps |
| `06_duplicate_step` | meldet denselben Step zweimal | `noMatchingPlan` nach 5 Steps |
| `07_early_stop` | bricht nach der Hälfte ab | beide Prozesse warten weiter |

Die Dateien sind von Hand editierbar. Jeder Step trägt neben `actionID` die
Lesehilfen `_connection` und `_actionType`; der Client wertet nur `actionID` und
`assemblyID` aus. Das Feld `terminal` (`"planComplete"`, `"noMatchingPlan"` oder
`null`) ist das, woran `run_scenarios.py` Erfolg misst — wer ein achtes Szenario
hinzufügt, muss es setzen, sonst wird die Datei als Fehler gezählt.

### 3.5 Schalter

**Reasoner** (`mocks/or_reasoner_mock.py`)

| Schalter | Default | Bedeutung |
|---|---|---|
| `--plan` | `data/plans/test_plan_1_3.json` | Eingabeplan |
| `--broker-host` / `--broker-port` | `localhost` / `1883` | Broker |
| `--store` | `index` | `index` (~5,8 MB) oder `plans` (~705 MB) |
| `--out-dir` | `generated` | Ablageverzeichnis |
| `--seed` | zufällig | reproduzierbare Planauswahl. `0` bedeutet zusätzlich: Plan 1 ist der initiale Plan (Blöcke in Originalreihenfolge, Layout 0) statt eines zufälligen. |
| `--emit-scenarios DIR` | — | Szenariodateien erzeugen und beenden, ohne MQTT |
| `--max-store-count` | `10000000` | oberhalb wird die Ablage verweigert |

**Client** (`mocks/assembly_client_mock.py`)

| Schalter | Default | Bedeutung |
|---|---|---|
| `--scenario` | *erforderlich* | Szenariodatei |
| `--broker-host` / `--broker-port` | `localhost` / `1883` | Broker |
| `--step-delay` | `0` | Pause vor jedem Step, für mitlesbare Läufe |

**Konverter** (`converter/resultat_to_json.py`)

```powershell
# einfaches Format
uv run python -m converter.resultat_to_json data/input/Resultat.txt \n  -o data/output/Resultat_output.json

# erweitertes Format mit components/parts/tool
uv run python -m converter.resultat_to_json data/input/Resultat.txt \n  -n data/input/Stossecke_Li_Annotationen_1.lp -o data/output/Resultat_output_2.json
```

### 3.6 Tests

```powershell
uv run python -m unittest discover -s tests -v
```

106 Tests, rund 2,5 Sekunden, kein Broker nötig. Der End-to-End-Lauf aus 3.2 ist die
Ergänzung dazu und braucht den Broker.

### 3.7 Einen eigenen Plan verwenden

```powershell
uv run python -m mocks.or_reasoner_mock --plan pfad/zu/plan.json
```

Der Plan muss valides JSON im Format aus 1.8 sein. Rechne vorher nach, wie groß der
Raum wird — er wächst multiplikativ mit jeder Verbindung, die mehrere gleichartige
Verbindungselemente hat:

```powershell
uv run python -c "from mocks import plan_space as p; \n  s = p.PlanSpace(p.strip_null_subjects(p.load_plan('pfad/zu/plan.json'))); print(s.count())"
```

---

## 4 Grenzen

Was hier steht, ist gemessen, nicht geschätzt.

**Der Planraum wächst multiplikativ.** Eine einzige zusätzliche Verbindung mit vier
Muttern hob ihn während der Entwicklung von 60 auf 138 240.

**Eine Mutter mehr an einer Verbindung ist die Wand:**

| Muttern | Layouts des Blocks | Zeit | Speicher |
|---|---|---|---|
| 4 (heute) | 576 | 0,01 s | 0,3 MB |
| 5 | 14 400 | 0,14 s | 7 MB |
| 6 | 518 400 | 5,5 s | 270 MB |
| 7 | 25 401 600 | 377 s | **15 GB** |

Deshalb die Layout-Obergrenze: oberhalb von 2 000 000 Layouts pro Block wird der
Aufbau verweigert, bevor etwas alloziert wird.

**`Resultat_output_2.json` als Eingabe ergibt 385 648 863 215 616 000 Pläne.** Der
Reasoner selbst käme damit rechnerisch zurecht — `count_matching` und
`sample_matching` zählen mit Ganzzahlen beliebiger Größe und zählen nie auf. Die
*Ablage* nicht: das wären rund sechzehn Exabyte JSONL. Sie verweigert daher:

```
[reasoner] Resultat_output_2.json hat 385648863215616000 Plaene - das liegt ueber
der Ablage-Obergrenze 10000000 fuer --store index. Es wird nichts geschrieben und
der Reasoner startet nicht.
```

**Die Permutationsregel ist für Pläne dieser Größenordnung nicht gedacht.** Wer sie
dort anwenden will, braucht zusätzlich die Vorrangbedingungen aus den ASP-Fakten,
die im Plan-JSON nicht enthalten sind — dann schrumpft die Zahl der *zulässigen*
Reihenfolgen drastisch.

---

## 5 Was bewusst offen ist

- `build_space()` ist nicht in `try/except` gefasst: ein Plan, der eine Obergrenze
  reißt, endet mit einem `ValueError`-Traceback statt der gepflegten Meldung. Der
  Fehler ist laut und nennt Verbindungsname und Zahl — nur unschöner.
- `run_scenarios.py` fängt `json.loads` auf den Szenariodateien nicht ab: eine
  wirklich korrupte Datei bricht den Lauf ab, statt als einzelnes Szenario zu
  scheitern. Die Dateien werden vom Reasoner erzeugt, nicht von Hand geschrieben.
- `read_index` hat keinen Aufrufer. Es bleibt trotzdem: es ist der ausführbare
  Beleg dafür, dass der Index eine verlustfreie Beschreibung ist.
