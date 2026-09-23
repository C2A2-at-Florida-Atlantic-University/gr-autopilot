"""Edit-ledger tests (no hardware)."""
from gr_autopilot.ledger import EditLedger, LedgerEntry


def test_append_and_read(tmp_path):
    led = EditLedger(tmp_path / "ledger.jsonl")
    led.append(LedgerEntry(1, "qpsk_v1", "baseline QPSK",
                           params={"sps": 4}, metrics={"BER": 1e-3, "EVM": 12.0, "SNR_est": 15.0},
                           verdict="kept"))
    led.append(LedgerEntry(2, "qam16_v1", "switched QPSK->16QAM",
                           metrics={"BER": 2e-2}, verdict="reverted"))
    rows = led.read()
    assert len(rows) == 2
    assert rows[0]["structure_id"] == "qpsk_v1"
    assert rows[1]["verdict"] == "reverted"
    assert rows[0]["metrics"]["BER"] == 1e-3


def test_next_iteration(tmp_path):
    led = EditLedger(tmp_path / "l.jsonl")
    assert led.next_iteration() == 1
    led.append(LedgerEntry(1, "s", "e"))
    led.append(LedgerEntry(7, "s", "e"))
    assert led.next_iteration() == 8


def test_append_dict(tmp_path):
    led = EditLedger(tmp_path / "l.jsonl")
    led.append({"iteration": 1, "structure_id": "x", "edit_description": "d", "metrics": {"ber": 0.0}})
    assert led.read()[0]["structure_id"] == "x"


def test_annotate(tmp_path):
    led = EditLedger(tmp_path / "l.jsonl")
    led.append(LedgerEntry(1, "s", "e"))
    led.annotate(1, "looked hopeless, early-stopped")
    rows = led.read()
    assert rows[-1]["verdict"] == "annotation"
    assert "hopeless" in rows[-1]["note"]


def test_compact_table_renders(tmp_path):
    led = EditLedger(tmp_path / "l.jsonl")
    assert "empty" in led.compact_table()
    led.append(LedgerEntry(1, "qpsk_v1", "baseline",
                           metrics={"BER": 1.2e-3, "EVM": 12.3, "SNR_est": 15.1}, verdict="kept"))
    table = led.compact_table()
    assert "structure" in table
    assert "qpsk_v1" in table
    assert "kept" in table


def test_the_compact_table_stamps_measurements_with_their_channel_and_nothing_else(tmp_path):
    """The channel column answers "what was this BER measured on", so only a BER gets one.

    A ledger that renders the error ratios but not the channel beneath them cannot show a
    frequency-avoidance episode at all: the jammed trials and the recovered ones are the same
    structure with different numbers and nothing on the row says why. Stamping the DECISION rows
    too would be the opposite error -- a retune's frequency is where it went, not where anything
    beside it was measured -- so those stay blank and keep their own sentence.
    """
    led = EditLedger(tmp_path / "l.jsonl")
    led.append(LedgerEntry(1, "qam16_link", "run 16qam", params={"center_freq_hz": 2.370e9},
                           metrics={"BER": 0.41, "EVM": 136.3, "SNR_est": -2.69}, verdict="run"))
    led.append(LedgerEntry(2, "", "sense 5 channels", params={"center_freq_hz": 2.370e9},
                           verdict="sensed"))
    led.append(LedgerEntry(3, "", "retune 2380.000 MHz",
                           params={"center_freq_hz": 2.380e9, "from_hz": 2.370e9},
                           verdict="retuned"))
    led.append(LedgerEntry(4, "qam16_link", "run 16qam", params={"center_freq_hz": 2.380e9},
                           metrics={"BER": 1.3e-4, "EVM": 14.2, "SNR_est": 16.9}, verdict="run"))
    header, *body = led.compact_table().splitlines()
    # Read the column itself rather than the line: a retune's own sentence says "2380.000 MHz",
    # so searching the whole row would pass on text that is not in the channel column at all.
    off = header.index("chan/MHz")
    chan = {r.split()[0]: r[off:off + len("chan/MHz")].strip() for r in body}

    # Trailing zeros are dropped: the column costs four characters on a whole megahertz.
    assert chan["1"] == "2370" and chan["4"] == "2380"
    # A sweep and a retune carry a centre frequency in their params and still get no stamp.
    assert chan["2"] == "" and chan["3"] == ""
    # The decision rows keep their own sentence, which is where their frequency belongs.
    assert "retune 2380.000 MHz" in next(r for r in body if r.startswith("3 "))


def test_a_recorded_iteration_is_all_there_or_not_there(tmp_path):
    """The durability contract changed in kind when storage moved to a database, and this test
    changed with it rather than being quietly dropped.

    The previous text format promised a "truncated but parseable" log: a crash mid-write lost at
    most the final line, and a half-written line was skipped on read. A database promises
    something stronger for writes that completed -- each is atomic, so a partially recorded
    iteration cannot be read back at all -- and something weaker for the file as a whole, since a
    corrupted database is not partially readable the way truncated text is.
    """
    led = EditLedger(tmp_path / "l.jsonl")
    led.append(LedgerEntry(1, "s", "e", metrics={"BER": 1e-3}))
    led.append(LedgerEntry(2, "s", "e2", metrics={"BER": 5e-4}))

    rows = led.read()
    assert len(rows) == 2
    # Every field of every row is present: no row can be half-written.
    for row in rows:
        assert set(row) >= {"iteration", "loop", "structure_id", "edit_description",
                            "verdict", "note", "params", "metrics"}
    assert rows[0]["metrics"]["BER"] == 1e-3


def test_a_jsonl_path_is_mapped_to_a_database_file(tmp_path):
    """The call sites predate the database and pass names like ``session.jsonl``. Writing a
    database into a file named for a text format would mislead anyone who went looking."""
    led = EditLedger(tmp_path / "session.jsonl")
    led.append(LedgerEntry(1, "s", "e"))
    assert led.path.suffix == ".db"
    assert led.path.exists()
    assert not (tmp_path / "session.jsonl").exists()
