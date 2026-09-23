"""Experiments, iterations and artifacts in the local database.

The capability this adds over the previous single flat file is grouping: an EXPERIMENT is a
first-class record, iterations belong to one, and a file the experiment produced can be recorded
against it. The previous format had no such notion -- iteration numbers simply climbed, so where
one run ended and the next began was not recoverable from the data.
"""
import json
import sqlite3
import threading

import pytest

from gr_autopilot.ledger.ledger import EditLedger, LedgerEntry, db_path_for
from gr_autopilot.ledger.store import ExperimentStore, import_jsonl


@pytest.fixture()
def store(tmp_path):
    s = ExperimentStore(tmp_path / "runs.db")
    yield s
    s.close()


# -- experiments -------------------------------------------------------------

def test_an_experiment_groups_its_iterations(store):
    a = store.start_experiment("sweep-a", goal="maximize efficiency", backend="numpy-sim")
    b = store.start_experiment("sweep-b")
    store.add_iteration(a, structure_id="qpsk", edit="baseline", metrics={"BER": 1e-3})
    store.add_iteration(a, structure_id="16qam", edit="climb", metrics={"BER": 2e-2})
    store.add_iteration(b, structure_id="bpsk", edit="baseline", metrics={"BER": 0.0})

    assert store.count_iterations(a) == 2
    assert store.count_iterations(b) == 1
    assert [r["structure_id"] for r in store.iterations(a)] == ["qpsk", "16qam"]


def test_iteration_numbering_restarts_per_experiment(store):
    """The flat file could not do this: its numbering was global, so two runs in one file were
    indistinguishable from one long run."""
    a, b = store.start_experiment("a"), store.start_experiment("b")
    assert store.add_iteration(a) == 1
    assert store.add_iteration(a) == 2
    assert store.add_iteration(b) == 1


def test_experiments_can_be_closed_and_listed(store):
    a = store.start_experiment("first")
    store.end_experiment(a)
    b = store.start_experiment("second")
    listed = store.list_experiments()
    assert [e.id for e in listed] == [b, a]        # most recent first
    assert store.get_experiment(a).running is False
    assert store.get_experiment(b).running is True


def test_goal_and_configuration_survive_a_round_trip(store):
    exp = store.start_experiment("x", goal="hold BER <= 1e-2", backend="gr-spec",
                                 config={"channels": [2.4e9, 2.402e9]})
    got = store.get_experiment(exp)
    assert got.goal == "hold BER <= 1e-2" and got.backend == "gr-spec"
    assert got.config["channels"] == [2.4e9, 2.402e9]


# -- iterations --------------------------------------------------------------

def test_metrics_are_both_queryable_and_preserved_whole(store):
    """The three headline measurements get their own columns so common questions are one query,
    while the full document is still kept -- a backend may report more than three numbers."""
    exp = store.start_experiment("x")
    store.add_iteration(exp, metrics={"BER": 3e-3, "EVM": 12.5, "SNR_est": 18.2,
                                      "frame_sync_sharpness": 70})
    row = store.iterations(exp)[0]
    assert row["metrics"]["frame_sync_sharpness"] == 70
    best = store.best(exp)
    assert best["metrics"]["BER"] == 3e-3


def test_best_finds_the_lowest_error_ratio(store):
    exp = store.start_experiment("x")
    for ber in (1e-2, 4e-4, 3e-3):
        store.add_iteration(exp, metrics={"BER": ber})
    assert store.best(exp)["metrics"]["BER"] == 4e-4


def test_best_is_none_when_nothing_measured_an_error_ratio(store):
    exp = store.start_experiment("x")
    store.add_iteration(exp, edit="annotation only")
    assert store.best(exp) is None


def test_next_seq_does_not_depend_on_history_length(store):
    """Appending used to require re-reading everything written so far; this is an indexed lookup."""
    exp = store.start_experiment("x")
    for _ in range(50):
        store.add_iteration(exp)
    assert store.next_seq(exp) == 51


def test_limit_returns_the_most_recent_rows_in_order(store):
    exp = store.start_experiment("x")
    for i in range(10):
        store.add_iteration(exp, edit=f"step{i}")
    rows = store.iterations(exp, limit=3)
    assert [r["edit_description"] for r in rows] == ["step7", "step8", "step9"]


# -- artifacts ---------------------------------------------------------------

def test_an_artifact_is_recorded_against_its_experiment(store, tmp_path):
    exp = store.start_experiment("x")
    store.add_iteration(exp)
    store.add_artifact(exp, kind="flowgraph.grc", path=tmp_path / "a.grc", iteration_seq=1,
                       meta={"modulation": "qpsk"})
    got = store.artifacts(exp, kind="flowgraph.grc")
    assert len(got) == 1
    assert got[0]["meta"]["modulation"] == "qpsk" and got[0]["iteration_seq"] == 1


def test_artifacts_are_scoped_to_one_experiment(store, tmp_path):
    a, b = store.start_experiment("a"), store.start_experiment("b")
    store.add_artifact(a, "flowgraph.grc", tmp_path / "a.grc")
    assert store.artifacts(b) == []


# -- the EditLedger facade ---------------------------------------------------

def test_edit_ledger_keeps_working_against_the_database(tmp_path):
    """Seventeen call sites construct ``EditLedger(path)``; the storage change must not reach
    any of them."""
    led = EditLedger(tmp_path / "s.jsonl")
    led.append(LedgerEntry(1, "qpsk", "baseline", metrics={"BER": 1e-3}, verdict="kept"))
    led.annotate(1, "looks fine")
    rows = led.read()
    assert len(rows) == 2 and len(led) == 2
    assert "kept" in led.compact_table()
    assert rows[1]["note"] == "looks fine"


def test_two_ledgers_on_one_path_continue_the_same_history(tmp_path):
    """The operator console rebuilds its ledger on reset; that must not fork the history into two
    parallel experiments."""
    first = EditLedger(tmp_path / "s.jsonl")
    first.append(LedgerEntry(1, "a", "e"))
    second = EditLedger(tmp_path / "s.jsonl")
    second.append(LedgerEntry(2, "b", "e"))
    assert second.experiment_id == first.experiment_id
    assert len(second.read()) == 2


def test_db_path_mapping():
    assert db_path_for("/tmp/x/session.jsonl").name == "session.db"
    assert db_path_for("/tmp/x/runs.db").name == "runs.db"


# -- importing the old format ------------------------------------------------

def test_legacy_log_imports_as_a_single_experiment(store, tmp_path):
    """The old format recorded no run boundaries and no timestamps, so a file accumulated over
    many sessions cannot be split back into them. Importing as one experiment is the only honest
    reading; inventing boundaries would fabricate history."""
    src = tmp_path / "old.jsonl"
    src.write_text("\n".join(json.dumps(
        {"iteration": i, "structure_id": "qpsk", "edit_description": f"step{i}",
         "loop": "outer", "metrics": {"BER": 1e-3}, "verdict": "kept"}) for i in range(1, 6)))
    exp = import_jsonl(store, src, name="legacy")
    assert store.count_iterations(exp) == 5
    assert store.get_experiment(exp).running is False
    assert "no run boundaries" in store.get_experiment(exp).config["note"]


def test_import_tolerates_the_truncated_final_line_the_old_format_allowed(store, tmp_path):
    src = tmp_path / "old.jsonl"
    src.write_text(json.dumps({"iteration": 1, "structure_id": "a", "edit_description": "x"})
                   + "\n" + '{"iteration": 2, "struct')     # crash mid-write
    exp = import_jsonl(store, src)
    assert store.count_iterations(exp) == 1


# -- concurrency -------------------------------------------------------------

def test_a_reader_does_not_block_a_writer(tmp_path):
    """Appends used to be serialized by the operating system and never blocked. The write-ahead
    log is what preserves that: the dashboard polls while a measurement loop is writing."""
    writer = ExperimentStore(tmp_path / "c.db")
    exp = writer.start_experiment("x")
    errors = []

    def poll():
        reader = ExperimentStore(tmp_path / "c.db")
        try:
            for _ in range(40):
                reader.iterations(exp, limit=10)
        except sqlite3.Error as exc:     # a lock contention failure would surface here
            errors.append(exc)
        finally:
            reader.close()

    t = threading.Thread(target=poll)
    t.start()
    for i in range(40):
        writer.add_iteration(exp, edit=f"step{i}", metrics={"BER": 1e-3})
    t.join(timeout=30)
    writer.close()
    assert not errors, errors


def test_each_thread_gets_its_own_connection(tmp_path):
    """This interpreter's sqlite3 reports thread safety level 1: sharing one connection across
    threads is not allowed, so the store must not do it."""
    s = ExperimentStore(tmp_path / "t.db")
    exp = s.start_experiment("x")
    seen = []

    def work():
        seen.append(s._conn())
        s.add_iteration(exp)

    threads = [threading.Thread(target=work) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert len({id(c) for c in seen}) == 3
    assert s.count_iterations(exp) == 3
    s.close()


def test_annotating_an_iteration_adds_a_row_rather_than_overwriting_it(tmp_path):
    """Regression guard. An annotation carries the sequence number of the iteration it refers
    to. A first version of the schema made (experiment, sequence) unique and wrote with INSERT OR
    REPLACE, so annotating iteration 1 silently destroyed iteration 1 and its measurements."""
    led = EditLedger(tmp_path / "s.jsonl")
    led.append(LedgerEntry(1, "qpsk", "baseline", metrics={"BER": 1e-3}, verdict="kept"))
    led.annotate(1, "the matched filter is doing the work here")

    rows = led.read()
    assert len(rows) == 2
    original, annotation = rows
    assert original["metrics"]["BER"] == 1e-3          # measurement survived
    assert original["verdict"] == "kept"
    assert annotation["verdict"] == "annotation"
    assert annotation["iteration"] == 1                 # refers back to it
