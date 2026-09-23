"""Read-only telemetry: a snapshot writer + a stdlib HTTP server + a static dashboard page.

Demo-critical (spec §3.4): a passive browser view of what the agent is doing — constellation,
spectrum, metrics vs. target, the edit-ledger timeline, and which control loop is active. All
control stays in chat; the dashboard only observes. No third-party deps (the bench host has no
pip): the server is ``http.server``, the page polls a JSON endpoint, and plots are drawn on a
canvas — swapping the spec's FastAPI/websockets/plotly for the stdlib equivalents.
"""
from gr_autopilot.telemetry.writer import TelemetryWriter

__all__ = ["TelemetryWriter"]
