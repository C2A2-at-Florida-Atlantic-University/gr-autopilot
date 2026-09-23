"""Model Context Protocol (MCP) over Streamable HTTP -- the daemon's agent-facing transport.

These tests cover the transport only; the protocol logic is shared with the stdio server and is
tested in ``test_mcp_stdio.py``. Two properties matter most here and are easy to get wrong:

1. ``GET /mcp`` must return 405, NOT the dashboard. The static file handler serves
   ``index.html`` for every unknown path, so a route registered after it would appear to work
   while returning a web page, and a test asserting only on the status code would still pass.
2. A cross-site browser request must be refused. A page on another origin can otherwise POST to a
   server on the loopback interface (a DNS-rebinding attack), and this endpoint drives radios.
"""
import json
import urllib.error
import urllib.request

import pytest

from gr_autopilot.daemon import build
from gr_autopilot.telemetry.server import serve
from gr_autopilot.tools.mcp_http import SESSION_HEADER, MCPHttpEndpoint
from gr_autopilot.tools.mcp_stdio import StdioMCPServer

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}}}


class Headers(dict):
    """Case-insensitive header mapping, like http.client's."""

    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def _endpoint(**kw):
    return MCPHttpEndpoint(StdioMCPServer(), **kw)


def _post(ep, payload, headers=None):
    body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
    return ep.post(body, Headers(headers or {}))


def _session(ep):
    status, headers, _ = _post(ep, INIT)
    assert status == 200
    return headers[SESSION_HEADER]


# -- session lifecycle -------------------------------------------------------

def test_initialize_issues_a_session_id():
    ep = _endpoint()
    status, headers, body = _post(ep, INIT)
    assert status == 200
    assert headers[SESSION_HEADER]
    assert json.loads(body)["result"]["serverInfo"]["name"] == "gr-autopilot"


def test_protocol_version_is_echoed_when_supported():
    ep = _endpoint()
    _, _, body = _post(ep, INIT)
    assert json.loads(body)["result"]["protocolVersion"] == "2025-06-18"


def test_calls_without_a_session_are_refused():
    ep = _endpoint()
    status, _, body = _post(ep, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert status == 400
    assert "Mcp-Session-Id" in json.loads(body)["error"]["message"]


def test_unknown_session_returns_404_so_the_client_reinitializes():
    """404 is how the specification tells a client its session expired; any other code invites an
    infinite retry loop against a server that will never accept the id."""
    ep = _endpoint()
    status, _, _ = _post(ep, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                         {SESSION_HEADER: "not-a-real-session"})
    assert status == 404


def test_delete_terminates_the_session():
    ep = _endpoint()
    sid = _session(ep)
    status, _, _ = ep.delete(Headers({SESSION_HEADER: sid}))
    assert status == 204
    status, _, _ = _post(ep, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                         {SESSION_HEADER: sid})
    assert status == 404


def test_session_can_be_disabled_for_simple_clients():
    ep = _endpoint(require_session=False)
    status, _, body = _post(ep, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert status == 200
    # Count comes from the manifest, not a literal: adding a tool must not fail this test.
    assert len(json.loads(body)["result"]["tools"]) == len(StdioMCPServer().tools)


# -- transport semantics -----------------------------------------------------

def test_tools_list_over_http_matches_the_stdio_surface():
    ep = _endpoint()
    sid = _session(ep)
    _, _, body = _post(ep, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, {SESSION_HEADER: sid})
    http_tools = {t["name"] for t in json.loads(body)["result"]["tools"]}
    assert http_tools == {t.name for t in StdioMCPServer().tools}


def test_a_notification_gets_202_with_an_empty_body():
    """A payload carrying nothing to answer must not produce an empty JSON-RPC envelope."""
    ep = _endpoint()
    sid = _session(ep)
    status, headers, body = _post(ep, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                  {SESSION_HEADER: sid})
    assert status == 202 and body == b""
    assert headers["Content-Length"] == "0"


def test_get_is_405_because_no_event_stream_is_offered():
    ep = _endpoint()
    status, headers, _ = ep.get(Headers({}))
    assert status == 405 and "POST" in headers["Allow"]


def test_malformed_json_is_a_parse_error():
    ep = _endpoint()
    status, _, body = _post(ep, b"{not json", {SESSION_HEADER: "x"})
    assert status == 400 and json.loads(body)["error"]["code"] == -32700


def test_oversized_body_is_refused():
    ep = _endpoint()
    status, _, _ = ep.post(b"x" * (1 << 21), Headers({}))
    assert status == 413


def test_a_tool_call_runs_the_real_service_over_http():
    ep = _endpoint()
    sid = _session(ep)
    h = {SESSION_HEADER: sid}
    _post(ep, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
               "params": {"name": "build_flowgraph", "arguments": {"spec": {
                   "structure_id": "q", "modulation": "qpsk",
                   "tx_chain": ["qpsk_mod", "rrc_pulse_shape"],
                   "rx_chain": ["rrc_matched_filter", "symbol_sync", "qpsk_demod"]}}}}, h)
    _post(ep, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
               "params": {"name": "run_flowgraph", "arguments": {"n_bits": 20000}}}, h)
    _, _, body = _post(ep, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                            "params": {"name": "get_metrics", "arguments": {}}}, h)
    metrics = json.loads(json.loads(body)["result"]["content"][0]["text"])
    assert metrics["n_bits"] == 20000 and 0.0 <= metrics["BER"] <= 1.0


# -- security ----------------------------------------------------------------

def test_cross_origin_browser_request_is_blocked():
    ep = _endpoint()
    status, _, _ = _post(ep, INIT, {"Origin": "https://evil.example"})
    assert status == 403


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8080", "http://localhost:3000",
                                    "https://localhost"])
def test_loopback_origins_are_allowed(origin):
    ep = _endpoint()
    status, _, _ = _post(ep, INIT, {"Origin": origin})
    assert status == 200


def test_no_origin_header_is_allowed_for_non_browser_clients():
    assert _post(_endpoint(), INIT)[0] == 200


def test_token_is_required_when_configured():
    ep = _endpoint(token="s3cret")
    assert _post(ep, INIT)[0] == 401
    assert _post(ep, INIT, {"Authorization": "Bearer wrong"})[0] == 401
    assert _post(ep, INIT, {"Authorization": "Bearer s3cret"})[0] == 200


def test_token_also_guards_delete():
    ep = _endpoint(token="s3cret")
    assert ep.delete(Headers({}))[0] == 401


# -- integration: routing inside the real HTTP server ------------------------

def _urlopen(req):
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_mcp_route_is_reachable_and_does_not_shadow_the_dashboard(tmp_path):
    endpoint, _svc, _srv = build()
    httpd = serve(None, None, port=8151, background=True, mcp=endpoint)
    try:
        # GET /mcp must be 405 -- NOT 200 with the dashboard, which the catch-all would serve.
        status, body = _urlopen(urllib.request.Request("http://127.0.0.1:8151/mcp"))
        assert status == 405
        assert b"<!doctype html" not in body.lower()

        # ...while the dashboard itself is still served at the root.
        status, page = _urlopen(urllib.request.Request("http://127.0.0.1:8151/"))
        assert status == 200 and b"gr-autopilot" in page

        req = urllib.request.Request("http://127.0.0.1:8151/mcp", data=json.dumps(INIT).encode(),
                                     headers={"Content-Type": "application/json"})
        r = urllib.request.urlopen(req, timeout=5)
        assert r.status == 200 and r.headers[SESSION_HEADER]
    finally:
        httpd.shutdown()


def test_daemon_build_wires_a_working_surface():
    endpoint, service, server = build(es_n0_db=9.0)
    assert len(server.tools) == len(StdioMCPServer().tools)
    # ``_channel`` is private on purpose: it is the hidden channel quality the agent must discover
    # by measurement, so there is deliberately no public accessor for it.
    assert service._channel["es_n0_db"] == 9.0
    status, _, body = endpoint.post(json.dumps(INIT).encode(), Headers({}))
    assert status == 200 and json.loads(body)["result"]["capabilities"]["tools"] is not None


def test_the_hidden_channel_never_crosses_the_http_surface():
    """The integrity claim must hold on the new transport too, not just over stdio: no response
    from any endpoint may disclose the operator-set channel quality."""
    sentinel = -12345.678
    endpoint, service, _server = build()
    service.set_channel(es_n0_db=sentinel)
    sid = _session(endpoint)
    h = {SESSION_HEADER: sid}
    seen = []
    for payload in ({"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "get_status", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                     "params": {"name": "describe_experiment",
                                "arguments": {"name": "link_only"}}}):
        seen.append(_post(endpoint, payload, h)[2].decode())
    blob = "".join(seen)
    assert "12345" not in blob and str(sentinel) not in blob


def test_loopback_clients_are_exempt_from_the_token_and_others_are_not():
    """A --token daemon bound to the network keeps working for the local client: the machine
    boundary is its auth. Every other peer must present the bearer."""
    ep = _endpoint(token="s3cret")
    body = json.dumps(INIT).encode()
    assert ep.post(body, Headers({}), client="127.0.0.1")[0] == 200
    assert ep.post(body, Headers({}), client="::1")[0] == 200
    assert ep.post(body, Headers({}), client="10.0.0.5")[0] == 401
    assert ep.post(body, Headers({"Authorization": "Bearer s3cret"}), client="10.0.0.5")[0] == 200
    assert ep.delete(Headers({}), client="127.0.0.1")[0] == 204
    assert ep.delete(Headers({}), client="10.0.0.5")[0] == 401
