"""The permission a run carries to create, replace or delete the appliance's box (rfc-006 §4.3).

A value that belongs to one invocation rather than to the stack: the
`operator-stack` driver sets it on the process it starts for `up --force` and
`up --replace`, and the hooks on the instance read it where they act on it
(framework/pulumi.md §3). Never stack configuration, which is committed and
would grant it to every later run. Here because the driver, a script, and the
component that registers the hooks both name it, and neither imports the
other (style/pulumi.md, "Layering").
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: The variable, and the one value that grants the permission.
ENV = 'KLUSTER_STATE_BACKEND_REPLACE'
GRANTED = '1'

#: What a refusal tells the operator to run instead.
REMEDY = 'operator-stack state-backend up --force'


def granted(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this process carries the permission: `ENV` is `GRANTED`, and nothing else is."""
    return (os.environ if environ is None else environ).get(ENV) == GRANTED
