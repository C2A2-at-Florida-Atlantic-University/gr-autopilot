"""The dependency-free MCP stdio server: protocol handshake, the tool surface, the integrity
split over the wire, and a real build->run->measure loop -- plus a subprocess check that the
actual stdin/stdout transport works the way an external MCP client (Claude Desktop) drives it.
No `mcp` SDK, no hardware."""
import json
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from gr_autopilot.flowgraph import FlowgraphSpec
from gr_autopilot.tools import (AutopilotService, InProcessClient, MCPToolError,
                                StdioMCPServer, tool_manifest)

REPO = Path(__file__).resolve().parents[1]


def _server(**channel):
    return StdioMCPServer(AutopilotService(channel=channel or {"es_n0_db": 12.0}))


def _req(mid, method, params=None):
    return {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}


# ---- JSON-RPC robustness / conformance --------------------------------------

def test_non_object_message_is_invalid_request_not_a_crash():
    # a valid JSON value that isn't an object (42, "x", [1]) must return -32600, never crash the loop
    srv = _server()
    for bad in (42, "x", None, True):
        resp = srv.handle(bad)
        assert resp["error"]["code"] == -32600
    # a batch containing a non-object element handles that element as -32600 too
    batch = srv.handle([42])
    assert batch and batch[0]["error"]["code"] == -32600


def test_empty_batch_is_invalid_request():
    assert _server().handle([])["error"]["code"] == -32600


def test_missing_required_arg_is_invalid_params():
    # describe_block requires 'name'; omitting it is -32602 invalid_params, not an 'internal' error
    srv = _server()
    resp = srv.handle(_req(1, "tools/call", {"name": "describe_block", "arguments": {}}))
    assert resp["error"]["code"] == -32602 and "required" in resp["error"]["message"]


def test_serve_survives_a_bad_line_and_keeps_serving():
    # a garbage line must not kill serve(); a following valid request still gets a response
    srv = _server()
    stdin = StringIO('not json\n' + json.dumps(_req(7, "ping")) + '\n')
    stdout = StringIO()
    srv.serve(stdin=stdin, stdout=stdout)
    out = [json.loads(ln) for ln in stdout.getvalue().splitlines() if ln.strip()]
    assert out[0]["error"]["code"] == -32700          # parse error for the garbage line
    assert out[1]["id"] == 7 and "result" in out[1]   # ...and the server kept serving


def test_non_finite_metric_serializes_as_valid_json():
    # inf/NaN must not reach the wire as bare Infinity/NaN (invalid JSON for strict clients)
    from gr_autopilot.tools.mcp_stdio import _dumps
    text = _dumps({"snr": float("inf"), "ber": float("nan"), "ok": 1.5})
    assert "Infinity" not in text and "NaN" not in text
    assert json.loads(text) == {"snr": None, "ber": None, "ok": 1.5}


# ---- manifest ---------------------------------------------------------------

def test_manifest_is_well_formed():
    tools = tool_manifest()
    assert len(tools) >= 20
    names = [t.name for t in tools]
    assert len(names) == len(set(names))                 # no dupes
    svc = AutopilotService()
    for t in tools:
        assert t.description and callable(t.handler)
        assert t.to_mcp()["inputSchema"]["type"] == "object"
        assert hasattr(svc, t.name)                       # every tool maps to a service method


# ---- handshake --------------------------------------------------------------

def test_initialize_and_tools_list():
    srv = _server()
    init = srv.handle(_req(1, "initialize"))["result"]
    assert init["protocolVersion"] == "2024-11-05"
    assert init["serverInfo"]["name"] == "gr-autopilot"
    assert "spectral efficiency" in init["instructions"].lower()
    assert init["capabilities"]["tools"] == {"listChanged": False}

    tl = srv.handle(_req(2, "tools/list"))["result"]["tools"]
    assert {"build_flowgraph", "run_flowgraph", "get_metrics", "start_bo_run"} <= {t["name"] for t in tl}
    assert all("inputSchema" in t and "description" in t for t in tl)


def test_protocol_version_is_echoed_when_supported():
    srv = _server()
    assert srv.handle(_req(1, "initialize", {"protocolVersion": "2025-06-18"}))["result"]["protocolVersion"] == "2025-06-18"
    assert srv.handle(_req(1, "initialize", {"protocolVersion": "1999-01-01"}))["result"]["protocolVersion"] == "2024-11-05"


def test_notifications_and_ping_and_unknown():
    srv = _server()
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert srv.initialized is True
    assert srv.handle(_req(3, "ping"))["result"] == {}
    assert srv.handle(_req(4, "resources/list"))["result"] == {"resources": []}
    err = srv.handle(_req(5, "no/such/method"))["error"]
    assert err["code"] == -32601
    assert srv.handle({"jsonrpc": "2.0", "method": "no/such/notification"}) is None  # id-less -> silent


# ---- the loop, via an in-process client ------------------------------------

def test_client_drives_build_run_measure():
    cli = InProcessClient(_server(es_n0_db=12.0))
    assert len(cli.list_tools()) >= 20
    assert "radio engineer" in cli.server_instructions.lower()

    spec = FlowgraphSpec.link("qpsk").to_dict()
    assert cli.call("validate", spec=spec)["ok"]
    assert cli.call("build_flowgraph", spec=spec)["structure_id"] == "qpsk_link"
    cli.call("run_flowgraph", n_bits=200_000)
    m = cli.call("get_metrics")
    assert m["BER"] <= 1e-3                      # QPSK at 12 dB comfortably meets target
    assert cli.call("read_edit_ledger", compact=False)  # the run was logged


def test_token_accounting_per_attempt():
    srv = _server(es_n0_db=12.0)
    cli = InProcessClient(srv)
    cli.call("describe_experiment", name="link_only")
    cli.call("build_flowgraph", spec=FlowgraphSpec.link("qpsk").to_dict())
    cli.call("run_flowgraph", n_bits=20_000)     # closes attempt 1
    cli.call("get_metrics")
    cli.call("build_flowgraph", spec=FlowgraphSpec.link("bpsk").to_dict())
    cli.call("run_flowgraph", n_bits=20_000)     # closes attempt 2
    ts = srv.token_stats()
    assert ts["attempts"] == 2                    # one attempt per run_flowgraph
    assert ts["calls"] == 6 and ts["total"] > 0
    assert len(ts["per_attempt"]) == 2 and all(t > 0 for t in ts["per_attempt"])
    assert ts["by_tool"]["run_flowgraph"] > 0 and ts["estimated"] is True
    assert ts["avg_per_attempt"] == round(ts["total"] / 2)


def test_on_run_flowgraph_hook_fires_once_per_run():
    srv = _server()
    fired = []
    srv.on_run_flowgraph = lambda s: fired.append(s.token_stats()["attempts"])
    cli = InProcessClient(srv)
    cli.call("build_flowgraph", spec=FlowgraphSpec.link("qpsk").to_dict())
    cli.call("run_flowgraph", n_bits=20_000)
    cli.call("get_metrics")                        # not a run -> hook must not fire
    assert fired == [1]                            # exactly once, attempt already closed


def test_tool_error_becomes_iserror_envelope():
    srv = _server()
    # reading metrics before any run -> a tool-level error carried as isError (not a JSON-RPC error)
    resp = srv.handle(_req(9, "tools/call", {"name": "get_metrics", "arguments": {}}))["result"]
    assert resp["isError"] is True
    assert json.loads(resp["content"][0]["text"])["error"]["code"] == "no_result"
    # the client surface raises it
    with pytest.raises(MCPToolError, match="no_result"):
        InProcessClient(srv).call("get_metrics")


# ---- integrity split over the tool surface ---------------------------------

def test_agent_claims_radios_but_not_grader_over_mcp():
    cli = InProcessClient(_server())
    assert cli.call("claim_device", device_id="pluto-a", role="transmitter")["role"] == "transmitter"
    assert cli.call("claim_device", device_id="pluto-b", role="receiver")["role"] == "receiver"
    with pytest.raises(MCPToolError, match="claim_denied"):
        cli.call("claim_device", device_id="pluto-b", role="grader")


def test_hidden_channel_never_in_status():
    cli = InProcessClient(_server(es_n0_db=17.3, phase_offset_rad=0.4))
    cli.call("build_flowgraph", spec=FlowgraphSpec.link("qpsk").to_dict())
    cli.call("run_flowgraph", n_bits=50_000)
    blob = json.dumps(cli.call("get_status"))
    assert "es_n0" not in blob and "phase_offset" not in blob  # the setting keys never marshal out


# ---- the real stdio transport ----------------------------------------------

def test_serve_reads_and_writes_ndjson():
    """serve() must consume newline-delimited JSON-RPC and emit one JSON response per line."""
    lines = "\n".join(json.dumps(m) for m in [
        _req(1, "initialize"),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},  # no response
        _req(2, "tools/list"),
    ])
    out = StringIO()
    _server().serve(stdin=StringIO(lines + "\n"), stdout=out)
    responses = [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]
    assert [r["id"] for r in responses] == [1, 2]                 # the notification produced nothing
    assert responses[0]["result"]["serverInfo"]["name"] == "gr-autopilot"


def test_subprocess_entrypoint_speaks_mcp():
    """python -m gr_autopilot.mcp_server over real pipes -- exactly how Claude Desktop drives it."""
    reqs = "\n".join(json.dumps(m) for m in [
        _req(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        _req(2, "tools/list"),
        _req(3, "tools/call", {"name": "list_devices", "arguments": {}}),
    ]) + "\n"
    proc = subprocess.run(
        [sys.executable, "-m", "gr_autopilot.mcp_server"],
        input=reqs, capture_output=True, text=True, timeout=60,
        cwd=str(REPO), env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"},
    )
    responses = {json.loads(ln)["id"]: json.loads(ln)
                 for ln in proc.stdout.splitlines() if ln.strip()}
    assert responses[1]["result"]["protocolVersion"] == "2024-11-05"
    assert len(responses[2]["result"]["tools"]) >= 20
    devices = json.loads(responses[3]["result"]["content"][0]["text"])
    assert [d["device_id"] for d in devices] == ["pluto-a", "pluto-b"]
