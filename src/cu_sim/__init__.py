"""cu_sim: a deliberately hostile stand-in for a legacy credit-union teller system.

"CoreLink Teller 4.6" is fictional. Every member, account and credential here is synthetic.
The markup is hostile on purpose: framesets, table layout, no ids or test ids, unassociated
labels, ``<font>`` tags, javascript: links and clickable table cells. Runtime faults can be
switched on through a control API that listens on a separate port, outside any agent allowlist.
"""

from cu_sim.app import FAULTS, SimServer, start

__all__ = ["FAULTS", "SimServer", "start"]
