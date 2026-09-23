"""The link backend seam.

A ``LinkBackend`` runs one transmitter->channel->receiver trial and returns aligned
transmitted/received bits and symbols for the framework-owned scoring layer to grade.
The same interface is implemented by:
  * ``NumpySimBackend`` — pure-numpy AWGN, theory-checkable, no GNU Radio (import here),
  * ``GrSimBackend``    — a real GNU Radio flowgraph through ``channels.channel_model``,
  * (later) ``PlutoBackend`` — two PlutoSDRs over a cabled/attenuated path.

Loop logic, optimization, and scoring depend only on this interface, so swapping
simulation for hardware changes nothing above the seam. GNU Radio is imported lazily
inside ``GrSimBackend`` so this package (and the numpy backend) import with no GNU Radio
installed.
"""
from gr_autopilot.link.backend import LinkBackend, LinkParams, LinkResult
from gr_autopilot.link.numpy_sim import NumpySimBackend

__all__ = ["LinkBackend", "LinkParams", "LinkResult", "NumpySimBackend"]
