"""Agent-authored skills (C5 self-improvement), no radios.

The agent discovers a gap-filling coded rung on the grader, test-gates it, and promotes it into its
own persisted vocabulary — and never scores its own promotion (all measurement is the framework grader).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from gr_autopilot.control.skill_author import SkillAuthor
from gr_autopilot.flowgraph.learned import LearnedSkill, LearnedSkillStore
from gr_autopilot.link.interference_sim import InterferenceSimBackend
from gr_autopilot.tools import AutopilotService, InProcessClient, StdioMCPServer

LADDER = ["bpsk", "qpsk", "16qam", "qpsk:conv_k3_r34", "16qam:conv_k3_r34"]


def _author(snr):
    return SkillAuthor(InterferenceSimBackend(clean_es_n0_db=snr), target_ber=1e-2, bits=40_000)


# -- discovery -----------------------------------------------------------------------------------

def test_authors_a_gap_filling_coded_rung_at_marginal_snr():
    prop = _author(7.0).author("x", LADDER)
    assert prop is not None
    assert ":" in prop.benchmark["modcod"]                 # a composite (coded) rung, not a base one
    assert prop.benchmark["gain"] > 0                      # beats the best base rung on efficiency
    assert prop.spec["coding"] and prop.spec["structure_id"] == "x"


def test_authors_nothing_when_a_base_rung_is_already_best():
    assert _author(20.0).author("x", LADDER) is None       # 16-QAM base wins -> nothing worth authoring


# -- the test gate -------------------------------------------------------------------------------

def test_gate_promotes_a_good_proposal_and_persists(tmp_path):
    auth = _author(7.0)
    prop = auth.author("coded", LADDER)
    store = LearnedSkillStore(tmp_path / "learned.jsonl")
    g = auth.promote(prop, store, min_gain=0.4)
    assert g.ok and "coded" in store
    assert "coded" in LearnedSkillStore(tmp_path / "learned.jsonl")   # reload from JSONL roundtrips


def test_gate_rejects_insufficient_gain(tmp_path):
    auth = _author(7.0)
    store = LearnedSkillStore(tmp_path / "learned.jsonl")
    g = auth.promote(auth.author("coded", LADDER), store, min_gain=1.0)  # demand more than +0.5
    assert not g.ok and len(store) == 0 and "gain" in g.reason


def test_gate_rejects_a_duplicate_name(tmp_path):
    auth = _author(7.0)
    store = LearnedSkillStore(tmp_path / "l.jsonl")
    auth.promote(auth.author("coded", LADDER), store, 0.4)
    assert not auth.promote(auth.author("coded", LADDER), store, 0.4).ok  # already in the vocabulary


def test_gate_rejects_none():
    assert not _author(20.0).promote(None, LearnedSkillStore(), 0.4).ok


# -- over the MCP tool surface -------------------------------------------------------------------

def _svc(snr):
    wd = Path(tempfile.mkdtemp())                          # isolate the learned-skills persistence
    svc = AutopilotService(backend=InterferenceSimBackend(clean_es_n0_db=snr),
                           channel={"clean_es_n0_db": snr}, ledger_path=wd / "s.jsonl")
    return svc, InProcessClient(StdioMCPServer(svc))


def test_author_and_promote_over_mcp_grows_and_reuses_vocabulary():
    _, cli = _svc(7.0)
    assert {"author_skill", "promote_skill"} <= {t["name"] for t in cli.list_tools()}
    n0 = len(cli.call("list_skills"))
    a = cli.call("author_skill", name="coded_qpsk", ladder=LADDER)
    assert a["authored"] and a["benchmark"]["gain"] > 0
    p = cli.call("promote_skill", name="coded_qpsk")
    assert p["promoted"] and "coded_qpsk" in p["vocabulary"]
    skills = cli.call("list_skills")
    assert len(skills) == n0 + 1 and any(s.get("learned") for s in skills)
    # the authored skill is immediately usable
    cli.call("build_flowgraph", spec=a["spec"])
    cli.call("run_flowgraph", n_bits=40_000)
    assert cli.call("get_metrics")["BER"] <= 1e-2


def test_author_over_mcp_never_leaks_the_hidden_channel():
    _, cli = _svc(7.0)
    blob = json.dumps(cli.call("author_skill", name="x", ladder=LADDER))
    for forbidden in ("clean_es_n0", "es_n0_db", "interferer"):
        assert forbidden not in blob                        # measured benchmark, not the hidden channel


def test_promote_without_authoring_is_an_error():
    _, cli = _svc(7.0)
    try:
        cli.call("promote_skill")
        assert False, "expected a tool error"
    except Exception as exc:
        assert "author" in str(exc).lower()
