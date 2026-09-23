"""Perception: render received signals to scope images and diagnose them — the agent *sees* the link.

The controller already reasons from scalar metrics (BER/EVM/SNR). This adds the modality a human RF
engineer actually uses: the picture. ``render`` turns recovered symbols into a constellation/spectrum
PNG; ``diagnose`` reads that picture (a fast heuristic here; a real vision-language model over the same
interface) into a named fault + the MCP action that fixes it — phase offset -> tune the carrier, a
ring -> the carrier is unlocked, a spur -> interference to avoid.
"""
from gr_autopilot.perception.diagnose import Diagnosis, HeuristicVision, diagnose_constellation
from gr_autopilot.perception.render import constellation_png, spectrum_png

__all__ = ["Diagnosis", "HeuristicVision", "diagnose_constellation",
           "constellation_png", "spectrum_png"]
