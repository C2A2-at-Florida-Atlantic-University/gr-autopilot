"""Experiments as things you name, switch between and rename -- not a daemon flag.

Three claims. First, the LAYOUT: one directory per experiment under a runs dir, named by a
filesystem-safe slug of the display name, holding the ledger, the snapshot and the exports.
Second, the GATE: with no experiment selected, every tool that measures or records refuses with
``no_experiment`` so the agent has to ask the operator for a name, while discovery still works.
Third, the RESET: starting or switching clears the session (structure, tuning, claims) and the
radios' RF state, runs the operator hook, and REPORTS what it did -- the same account reaches the
agent over MCP and the dashboard over HTTP.
"""
import json
import urllib.error
import urllib.request

import pytest

from gr_autopilot.ledger.manager import ExperimentError, ExperimentManager, slugify
from gr_autopilot.telemetry.server import serve
from gr_autopilot.tools.service import AutopilotService, ToolError

BPSK = {"structure_id": "bpsk_link", "modulation": "bpsk",
        "tx_chain": ["bpsk_mod", "rrc_pulse_shape"],
        "rx_chain": ["agc", "rrc_matched_filter", "symbol_sync", "costas_carrier", "bpsk_demod"]}


# ---- layout ---------------------------------------------------------------------------------

def test_slug_is_filesystem_safe_and_stable():
    assert slugify("AMC ladder @ 2370") == "amc-ladder-2370"
    assert slugify("  Mini Test 1  ") == "mini-test-1"
    assert slugify("AMC/2370") == "amc-2370"        # a separator never escapes the runs dir
    for bad in ("", "   ", "..", "."):
        with pytest.raises(ExperimentError):
            slugify(bad)


def test_manager_creates_one_directory_per_experiment(tmp_path):
    m = ExperimentManager(tmp_path)
    led, info = m.create("Mini Test 1", goal="hold BER", backend="sim")
    assert (tmp_path / "mini-test-1" / "session.db").exists()
    assert info.name == "Mini Test 1" and info.slug == "mini-test-1" and info.goal == "hold BER"
    assert m.snapshot_path("mini-test-1") == tmp_path / "mini-test-1" / "telemetry.json"
    assert m.flowgraphs_dir("mini-test-1") == tmp_path / "mini-test-1" / "flowgraphs"
    with pytest.raises(ExperimentError):          # same slug, refused
        m.create("mini test 1")
    led.store.close()


def test_manager_rename_moves_the_directory_and_keeps_history(tmp_path):
    from gr_autopilot.ledger.ledger import LedgerEntry
    m = ExperimentManager(tmp_path)
    led, _ = m.create("first")
    led.append(LedgerEntry(1, "bpsk", "run bpsk", metrics={"BER": 0.0}, verdict="run"))
    info = m.rename(led, "Second Name")
    assert info.slug == "second-name" and not (tmp_path / "first").exists()
    led2, info2 = m.open("second name")
    assert info2.iterations == 1 and info2.name == "Second Name"
    led2.store.close()
    assert [i.slug for i in m.list()] == ["second-name"]


# ---- the gate --------------------------------------------------------------------------------

def test_nothing_selected_refuses_measurement_but_not_discovery(tmp_path):
    svc = AutopilotService(experiment=None, runs_dir=tmp_path)
    cur = svc.current_experiment()
    assert cur["selected"] is False and "ask the operator" in cur["next"]
    for call in (lambda: svc.build_flowgraph(BPSK),
                 lambda: svc.claim_device("pluto-a", "transmitter"),
                 lambda: svc.run_flowgraph(n_bits=2000),
                 lambda: svc.read_edit_ledger(),
                 lambda: svc.export_flowgraph()):
        with pytest.raises(ToolError) as e:
            call()
        assert e.value.code == "no_experiment"
    assert svc.list_skills() and len(svc.list_devices()) == 2   # discovery still answers
    assert svc.snapshot_path is None and svc.artifacts_dir is None


def test_default_named_experiment_keeps_library_use_working(tmp_path):
    svc = AutopilotService(runs_dir=tmp_path)            # experiment defaults to "session"
    assert svc.current_experiment()["selected"] and svc.experiment_dir == tmp_path / "session"
    svc.build_flowgraph(BPSK)
    svc.run_flowgraph(n_bits=2000)
    assert len(svc.ledger.read()) == 1


def test_ledger_path_compat_names_the_experiment_after_its_directory(tmp_path):
    svc = AutopilotService(ledger_path=tmp_path / "mini-test-9" / "session.jsonl")
    assert svc.current_experiment()["name"] == "mini-test-9"
    assert svc.snapshot_path == tmp_path / "mini-test-9" / "telemetry.json"


# ---- the reset --------------------------------------------------------------------------------

def test_start_and_switch_reset_the_session_and_report_it(tmp_path):
    svc = AutopilotService(experiment=None, runs_dir=tmp_path)
    seen = []
    svc.on_reset = lambda reason: (seen.append(reason), ["interferer was not armed"])[1]

    r = svc.start_experiment("AMC ladder", goal="top rung at 1e-2")
    assert r["action"] == "started" and r["experiment"]["slug"] == "amc-ladder"
    svc.claim_device("pluto-a", "transmitter")
    svc.build_flowgraph(BPSK)
    svc.run_flowgraph(n_bits=2000)
    assert svc.get_status()["structure_id"] == "bpsk_link"

    r2 = svc.start_experiment("second")
    rep = r2["reset"]
    assert "built structure 'bpsk_link' discarded" in rep["session"]
    assert "last measurement cleared" in rep["session"]
    assert any("pluto-a (transmitter)" in line for line in rep["session"])
    assert rep["operator"] == ["interferer was not armed"] and seen[-1].startswith("started")
    st = svc.get_status()
    assert st["structure_id"] is None and st["has_result"] is False
    assert all(c["owner"] != "agent" for c in st["claims"])   # only the framework grader remains
    assert svc.experiment_dir == tmp_path / "second"

    r3 = svc.switch_experiment("amc ladder")
    assert r3["action"] == "switched" and r3["experiment"]["iterations"] == 1
    assert svc.switch_experiment("AMC ladder")["action"] == "unchanged"

    with pytest.raises(ToolError) as e:
        svc.start_experiment("second")
    assert e.value.code == "experiment_exists"
    with pytest.raises(ToolError) as e:
        svc.switch_experiment("never-made")
    assert e.value.code == "unknown_experiment"


def test_rename_keeps_the_built_link_and_the_history(tmp_path):
    svc = AutopilotService(experiment=None, runs_dir=tmp_path)
    svc.start_experiment("draft")
    svc.build_flowgraph(BPSK)
    svc.run_flowgraph(n_bits=2000)
    r = svc.rename_experiment("Final name", goal="renamed")
    assert r["experiment"]["slug"] == "final-name" and r["experiment"]["goal"] == "renamed"
    assert svc.get_status()["structure_id"] == "bpsk_link"       # nothing reset
    assert len(svc.ledger.read()) == 1 and svc.experiment_dir == tmp_path / "final-name"
    assert not (tmp_path / "draft").exists()


# ---- over HTTP, the dashboard's path ---------------------------------------------------------

def _get(url):
    return json.loads(urllib.request.urlopen(url, timeout=5).read())


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_dashboard_can_name_switch_and_rename_over_http(tmp_path):
    svc = AutopilotService(experiment=None, runs_dir=tmp_path)
    httpd = serve(lambda: svc.ledger_db_path, lambda: svc.snapshot_path, port=8171,
                  background=True, artifacts_dir=lambda: svc.artifacts_dir, experiments=svc)
    base = "http://127.0.0.1:8171"
    try:
        data = _get(base + "/data")
        assert data["experiment"] is None and data["ledger"] == []      # the page asks for a name
        listing = _get(base + "/experiments")
        assert listing == {"experiments": [], "current": None, "runs_dir": str(tmp_path)}

        code, out = _post(base + "/experiments", {"action": "start", "name": "From the GUI", "goal": "g"})
        assert code == 200 and out["experiment"]["slug"] == "from-the-gui" and "reset" in out
        assert _get(base + "/data")["experiment"]["name"] == "From the GUI"
        assert _get(base + "/experiments")["current"] == "from-the-gui"

        code, out = _post(base + "/experiments", {"action": "start", "name": "from the gui"})
        assert code == 400 and out["code"] == "experiment_exists"

        code, out = _post(base + "/experiments", {"action": "rename", "name": "Renamed"})
        assert code == 200 and out["experiment"]["slug"] == "renamed"

        _post(base + "/experiments", {"action": "start", "name": "other"})
        code, out = _post(base + "/experiments", {"action": "switch", "name": "renamed"})
        assert code == 200 and out["action"] == "switched"
        assert {e["slug"] for e in _get(base + "/experiments")["experiments"]} == {"renamed", "other"}

        code, out = _post(base + "/experiments", {"action": "explode", "name": "x"})
        assert code == 400
    finally:
        httpd.shutdown()


def test_deleting_an_experiment_removes_it_and_says_what_it_removed(tmp_path):
    """Delete is the one lifecycle verb that destroys, so it reports what it destroyed.

    "Deleted" on its own is not checkable. The iteration count is the only measure of what a run
    was worth and it cannot be looked up afterwards, so it is read off the experiment BEFORE the
    directory goes and handed back with the answer.
    """
    m = ExperimentManager(tmp_path)
    led, _ = m.create("Old Sweep", goal="superseded", backend="sim")
    assert (tmp_path / "old-sweep" / "session.db").exists()
    led.store.close()          # the handle must be released before the directory goes

    gone = m.delete("Old Sweep")
    assert gone.name == "Old Sweep" and gone.slug == "old-sweep"
    assert not (tmp_path / "old-sweep").exists()
    assert m.list() == []

    with pytest.raises(ExperimentError):
        m.delete("old-sweep")               # and again is not a silent success


def test_the_current_experiment_cannot_be_deleted_from_under_the_session(tmp_path):
    """Deleting the ledger the session is writing into is not a tidy-up, it is a fault.

    The store holds an open SQLite handle on that directory; remove it and every later append
    fails in a way that looks like the disk or the radios misbehaving. So the refusal is explicit
    and names the way out -- switch first, which closes the store and resets the session.
    """
    svc = AutopilotService(experiment=None, runs_dir=tmp_path)
    svc.start_experiment("Keep This")
    svc.start_experiment("Spare")                 # current is now "spare"

    with pytest.raises(ToolError) as exc:
        svc.delete_experiment("Spare")
    assert exc.value.code == "experiment_in_use"
    assert (tmp_path / "spare" / "session.db").exists()

    out = svc.delete_experiment("Keep This")      # the one we are NOT in
    assert out["action"] == "deleted" and out["experiment"]["slug"] == "keep-this"
    assert not (tmp_path / "keep-this").exists()
    assert svc.current_experiment()["slug"] == "spare"


def test_delete_is_an_operator_verb_with_no_tool_behind_it(tmp_path):
    """The agent measures and records; it must not be able to destroy a previous run's data.

    The console reaches delete over POST /experiments under the same bearer token as the other
    lifecycle verbs. There is deliberately no MCP tool, so nothing the model can call removes an
    experiment -- an agent that can delete the evidence it was asked to produce is not one whose
    reports can be checked.
    """
    from gr_autopilot.tools.manifest import tool_manifest
    names = {t.name for t in tool_manifest()}
    assert "delete_experiment" not in names
    assert {"start_experiment", "switch_experiment", "rename_experiment"} <= names


def test_dashboard_can_delete_a_saved_experiment_over_http(tmp_path):
    svc = AutopilotService(experiment=None, runs_dir=tmp_path)
    httpd = serve(lambda: svc.ledger_db_path, lambda: svc.snapshot_path, port=8172,
                  background=True, artifacts_dir=lambda: svc.artifacts_dir, experiments=svc)
    base = "http://127.0.0.1:8172"
    try:
        _post(base + "/experiments", {"action": "start", "name": "Scratch"})
        _post(base + "/experiments", {"action": "start", "name": "Current"})
        assert {e["slug"] for e in _get(base + "/experiments")["experiments"]} == {"scratch", "current"}

        code, out = _post(base + "/experiments", {"action": "delete", "name": "current"})
        assert code == 400 and out["code"] == "experiment_in_use"   # the one we are in

        code, out = _post(base + "/experiments", {"action": "delete", "name": "scratch"})
        assert code == 200 and out["action"] == "deleted"
        assert out["experiment"]["name"] == "Scratch"
        assert {e["slug"] for e in _get(base + "/experiments")["experiments"]} == {"current"}
        assert not (tmp_path / "scratch").exists()

        code, out = _post(base + "/experiments", {"action": "delete", "name": "never-existed"})
        assert code == 400 and out["code"] == "unknown_experiment"
    finally:
        httpd.shutdown()


def test_a_portal_on_the_lan_names_experiments_with_the_access_token():
    """One credential for the whole write surface: the bearer token on ``--token``.

    A browser on the bench host sends nothing -- loopback is exempt, because a local process can
    read the token out of the daemon's command line anyway. From anywhere else the same token
    that opens /mcp opens this, and nothing else does.
    """
    from gr_autopilot.telemetry.server import _may_operate
    from gr_autopilot.tools.mcp_http import MCPHttpEndpoint

    mcp = MCPHttpEndpoint(server=None, token="bearer-secret")
    lan = "10.0.0.136"
    assert _may_operate({}, "127.0.0.1", mcp)                                  # bench host
    assert _may_operate({"Authorization": "Bearer bearer-secret"}, lan, mcp)
    assert not _may_operate({}, lan, mcp)
    assert not _may_operate({"Authorization": "Bearer wrong"}, lan, mcp)
    assert not _may_operate({"Authorization": "bearer-secret"}, lan, mcp)      # no scheme
    # The separate operator credential is gone; its header is not a second way in.
    assert not _may_operate({"X-Operator-Password": "bearer-secret"}, lan, mcp)
    assert not _may_operate({"X-Operator-Token": "bearer-secret"}, lan, mcp)
    # No MCP endpoint at all (a script serving a recording): nothing to check against.
    assert _may_operate({}, lan, None)


def test_a_refused_post_says_which_way_the_check_failed():
    """'Refused' on its own sends the operator round in circles: a header their browser never
    sent and a value that does not match look identical from the dashboard, and they have
    opposite fixes. The reason names which one it was -- without echoing the token."""
    from gr_autopilot.telemetry.server import _why_unauthorized

    secret = "bearer-secret-value"
    absent = _why_unauthorized({})
    assert "no Authorization header was sent" in absent

    # Anyone still sending the operator credential that was removed is told what to send instead.
    legacy = _why_unauthorized({"X-Operator-Password": "whatever"})
    assert "was removed" in legacy and "Authorization: Bearer" in legacy

    scheme = _why_unauthorized({"Authorization": secret})
    assert "not a Bearer credential" in scheme

    wrong = _why_unauthorized({"Authorization": f"Bearer {secret}"})
    assert "--token" in wrong

    for msg in (absent, legacy, scheme, wrong):
        assert secret not in msg                       # never echo the secret
        assert str(len(secret)) not in msg             # nor its length


def test_a_refused_post_does_not_desync_the_connection():
    """A POST rejected before its body is read leaves that body in the socket, and HTTP/1.1
    keep-alive then parses it as the NEXT request line: one refused write turns every following
    request on that connection into 'Bad request syntax'. The operator sees a burst of 400s on
    requests they never malformed, which buries the real refusal. The body must be drained."""
    import re
    import socket

    class _Experiments:
        def start_experiment(self, name, goal):     # never reached: the POST is refused first
            raise AssertionError("refused request must not reach the manager")

    httpd = serve(None, None, port=8172, background=True, experiments=_Experiments())
    try:
        body = b'{"action":"start","name":"Staircase Demo","goal":""}'
        sock = socket.create_connection(("127.0.0.1", 8172), timeout=5)
        sock.sendall(b"POST /experiments HTTP/1.1\r\nHost: 127.0.0.1:8172\r\n"
                     b"Origin: http://elsewhere.example\r\n"          # refused: cross-origin
                     b"Content-Type: application/json\r\n"
                     b"Content-Length: %d\r\n\r\n" % len(body) + body)
        sock.sendall(b"GET /devices HTTP/1.1\r\nHost: 127.0.0.1:8172\r\n\r\n")
        sock.settimeout(3)
        seen = b""
        try:
            while len(seen) < 4096:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                seen += chunk
        except socket.timeout:
            pass
        sock.close()
    finally:
        httpd.shutdown()

    # The second status line is glued straight onto the first response's body, so scan rather
    # than split on CRLF.
    codes = re.findall(rb"HTTP/1\.1 (\d\d\d)", seen)
    assert codes[:1] == [b"403"], seen[:200]
    assert len(codes) == 2, f"the second request got no clean answer: {seen[:400]!r}"
    assert codes[1] != b"400", f"the refused body was parsed as the next request: {seen[:400]!r}"


def test_daemon_build_starts_unselected_and_telemetry_follows_the_experiment(tmp_path):
    from gr_autopilot.daemon import build
    endpoint, service, server = build(experiment=None, runs_dir=tmp_path)
    assert service.current_experiment()["selected"] is False

    def call(tool, **args):
        r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}})
        return json.loads(r["result"]["content"][0]["text"]), bool(r["result"].get("isError"))

    out, err = call("build_flowgraph", spec=BPSK)
    assert err and out["error"]["code"] == "no_experiment"
    out, err = call("start_experiment", name="Named by the operator")
    assert not err and out["experiment"]["slug"] == "named-by-the-operator"
    call("build_flowgraph", spec=BPSK)
    out, err = call("run_flowgraph", n_bits=2000)
    assert not err
    snap = tmp_path / "named-by-the-operator" / "telemetry.json"
    assert snap.exists() and json.loads(snap.read_text())["structure"]["modcod"] == "bpsk"
