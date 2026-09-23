"""The operator surface (``/control``): who reaches it, and what it is willing to say.

There is one secret, the bearer token, and it guards the network. There is deliberately no
second, operator-only credential guarding the surface against the AGENT: this bench is driven
over the network, the agent is handed the bearer token in order to call tools at all, and one
credential cannot separate two principals. Two things have to stay true anyway, and this file is
what holds them:

  * ``/control`` is not simply open. The daemon can key a HackRF, and it is bound to a LAN, so
    reaching it from off the bench host still takes the token.
  * The hidden channel quality is not readable from it. The agent can now reach this endpoint, so
    the guarantee that it is never TOLD the answer it is supposed to measure has to be enforced
    by what the endpoint returns rather than by who is asking.
"""
import json
import urllib.error
import urllib.request

from gr_autopilot.telemetry.server import ControlState, serve
from gr_autopilot.tools.mcp_http import MCPHttpEndpoint

TOKEN = "bearer-secret"


def _req(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except ValueError:
            return exc.code, {}


def test_the_hidden_channel_is_not_readable_from_the_operator_surface(tmp_path):
    """The one guarantee that survives collapsing the two credentials into one.

    ``es_n0_db`` is the operator's hidden channel setpoint. The telemetry writer strips it from
    ``/data`` and every tool response is scanned for it, because an agent told the channel quality
    has not measured anything. ``GET /control`` is a third way to the same number, and with a
    shared credential the agent can reach it -- so the field is removed from the response rather
    than protected by who asked.
    """
    control = ControlState(es_n0_db=13.5, tx_atten_db=38.0)
    httpd = serve(None, None, port=8175, background=True, control=control, webroot=tmp_path,
                  mcp=MCPHttpEndpoint(server=None, token=TOKEN))
    try:
        code, state = _req("http://127.0.0.1:8175/control")        # loopback: exempt
        assert code == 200
        assert "es_n0_db" not in state
        assert state["tx_atten_db"] == 38.0                        # the rest is the operator's
        assert "13.5" not in json.dumps(state)
    finally:
        httpd.shutdown()


def test_the_operator_surface_still_needs_the_token_from_off_the_bench_host(tmp_path):
    """The daemon can key a HackRF and is bound to a LAN. 'One credential' does not mean 'none'.

    Loopback cannot be exercised as an outsider from this process, so the check here is the gate
    function the handler calls, driven with the addresses the handler would pass it.
    """
    from gr_autopilot.telemetry.server import _may_operate

    mcp = MCPHttpEndpoint(server=None, token=TOKEN)
    lan = "10.0.0.136"
    assert not _may_operate({}, lan, mcp)
    assert not _may_operate({"Authorization": "Bearer wrong"}, lan, mcp)
    assert _may_operate({"Authorization": f"Bearer {TOKEN}"}, lan, mcp)


def test_the_operator_surface_reads_and_writes_under_one_credential(tmp_path):
    """GET reports what POST set, and both answer to the same token."""
    control = ControlState()
    httpd = serve(None, None, port=8176, background=True, control=control, webroot=tmp_path,
                  mcp=MCPHttpEndpoint(server=None, token=TOKEN))
    base = "http://127.0.0.1:8176/control"
    try:
        code, _ = _req(base, method="POST", body={"target_ber": 1e-3},
                       headers={"Authorization": f"Bearer {TOKEN}"})
        assert code == 200 and control.get()["target_ber"] == 1e-3
        code, state = _req(base, headers={"Authorization": f"Bearer {TOKEN}"})
        assert code == 200 and state["target_ber"] == 1e-3
    finally:
        httpd.shutdown()


def test_a_removed_credential_is_not_a_silent_failure(tmp_path):
    """Anyone whose script still sends the operator header gets told what to send instead,
    rather than a bare 403 that reads as 'wrong value'."""
    httpd = serve(None, None, port=8177, background=True, control=ControlState(), webroot=tmp_path,
                  mcp=MCPHttpEndpoint(server=None, token=TOKEN))
    try:
        # Driven through the gate function, since this process reaches the server over loopback
        # and loopback is exempt from the token by design.
        from gr_autopilot.telemetry.server import _why_unauthorized
        why = _why_unauthorized({"X-Operator-Password": TOKEN})
        assert "was removed" in why and "Authorization: Bearer" in why
    finally:
        httpd.shutdown()
