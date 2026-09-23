"""The edit ledger (spec §9).

An append-only record, one entry per iteration, that is simultaneously the agent's working
memory and the paper's convergence data. Storage is a local SQLite database (``ledger/store.py``)
grouped into experiments; a ``.jsonl`` path is still accepted and mapped to a sibling ``.db``.
The ledger is a legibility/design choice, not a claimed research contribution.
"""
from gr_autopilot.ledger.ledger import EditLedger, LedgerEntry

__all__ = ["EditLedger", "LedgerEntry"]
