"""The daemon's console and log file: quiet about what does not matter, explicit about what does.

Three claims. A peer dropping a keep-alive connection -- what a browser on another machine does
constantly -- must not print a traceback (it used to, one per connection). Every tool call, every
operator command and every experiment change must leave one line naming who, what and how long.
And ``configure`` must be safe to call twice, because tests and a re-entrant main both do.
"""
import json
import logging
import socket
import urllib.request

import pytest

from gr_autopilot import logsetup
from gr_autopilot.telemetry.server import serve
from gr_autopilot.tools.mcp_stdio import StdioMCPServer
from gr_autopilot.tools.service import AutopilotService


@pytest.fixture(autouse=True)
def _reset_root_handlers():
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)
            h.close()


def test_configure_is_idempotent_and_writes_a_rotating_file(tmp_path):
    path = logsetup.configure("info", tmp_path / "logs" / "autopilotd.log")
    path2 = logsetup.configure("debug", tmp_path / "logs" / "autopilotd.log")
    assert path == path2 == tmp_path / "logs" / "autopilotd.log"
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_gr_autopilot_configured", False)]
    assert len(ours) == 2                                  # one console, one file -- not four
    logging.getLogger("gr_autopilot.test").debug("a debug line")
    logging.getLogger("gr_autopilot.test").info("an info line")
    for h in ours:
        h.flush()
    text = path.read_text()
    assert "an info line" in text and "a debug line" in text   # the file always records debug
    assert " test " in text                                   # component column, prefix stripped
    with pytest.raises(ValueError):
        logsetup.configure("loud")


def test_a_dropped_connection_is_debug_not_a_traceback(tmp_path, capfd, caplog):
    svc = AutopilotService(experiment=None, runs_dir=tmp_path)
    httpd = serve(lambda: svc.ledger_db_path, lambda: svc.snapshot_path, port=8181,
                  background=True, experiments=svc)
    try:
        with caplog.at_level(logging.DEBUG, logger="gr_autopilot.http"):
            # open a keep-alive connection, get one response, then reset it the way a browser
            # tab does: SO_LINGER 0 turns close() into RST rather than FIN.
            s = socket.create_connection(("127.0.0.1", 8181), timeout=5)
            s.sendall(b"GET /data HTTP/1.1\r\nHost: x\r\n\r\n")
            assert s.recv(4096).startswith(b"HTTP/1.1 200")
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            s.close()
            # a second request proves the server is still serving after the reset
            data = json.loads(urllib.request.urlopen("http://127.0.0.1:8181/data", timeout=5).read())
            assert "ledger" in data
        err = capfd.readouterr().err
        assert "Traceback" not in err and "ConnectionResetError" not in err
    finally:
        httpd.shutdown()


def test_every_tool_call_leaves_one_line(tmp_path, caplog):
    svc = AutopilotService(runs_dir=tmp_path)
    srv = StdioMCPServer(svc)

    def call(tool, **args):
        return srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}})

    with caplog.at_level(logging.INFO, logger="gr_autopilot.mcp"):
        call("list_skills")
        call("switch_experiment", name="does-not-exist")
    msgs = [r.getMessage() for r in caplog.records if r.name == "gr_autopilot.mcp"]
    assert any(m.startswith("tool list_skills ok (") and "ms)" in m for m in msgs), msgs
    assert any(m.startswith("tool switch_experiment -> error unknown_experiment") and
               "name=does-not-exist" in m for m in msgs), msgs


def test_experiment_changes_over_http_are_logged_with_the_caller(tmp_path, caplog):
    svc = AutopilotService(experiment=None, runs_dir=tmp_path)
    httpd = serve(lambda: svc.ledger_db_path, lambda: svc.snapshot_path, port=8182,
                  background=True, experiments=svc)
    try:
        with caplog.at_level(logging.INFO):
            req = urllib.request.Request("http://127.0.0.1:8182/experiments", method="POST",
                                         data=json.dumps({"action": "start", "name": "logged run"}).encode(),
                                         headers={"Content-Type": "application/json"})
            assert urllib.request.urlopen(req, timeout=5).status == 200
        http_lines = [r.getMessage() for r in caplog.records if r.name == "gr_autopilot.http"]
        svc_lines = [r.getMessage() for r in caplog.records if r.name == "gr_autopilot.service"]
        assert any("experiment start 'logged run' from 127.0.0.1 -> started" in m for m in http_lines), http_lines
        assert any(m.startswith("experiment started: 'logged run'") and "reset:" in m for m in svc_lines), svc_lines
    finally:
        httpd.shutdown()


def test_summarize_args_keeps_the_identifying_fields_only():
    s = logsetup.summarize_args({"spec": {"structure_id": "bpsk_link", "modulation": "bpsk",
                                          "tx_chain": ["a"] * 50}, "n_bits": 50000})
    assert s == "n_bits=50000, spec=bpsk_link/bpsk"
    assert len(logsetup.summarize_args({"name": "x" * 500})) <= 160
