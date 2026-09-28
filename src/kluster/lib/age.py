"""What code outside the `credentials` package needs to know about the `age` tool.

The tool's name, and what its ASCII armor looks like. Both the escrow
(`kluster.scripts.credentials.age`) and the appliance's dump and restore
(`kluster.lib.state_backend.state`) shell out to the same pinned binary and
read the same armor, so the two facts live below both of them. Everything
that does something with the tool -- key generation, the recipient check,
the text-in, text-out wrapper the escrow uses -- stays with its caller.
"""

from __future__ import annotations

#: The pinned binary (`mise.toml`). Named rather than inlined so a failure can
#: say which tool was missing and where it is pinned.
BINARY = 'age'

#: ASCII armor, because a ciphertext in the escrow is a file git carries.
ARMOR_BEGIN = '-----BEGIN AGE ENCRYPTED FILE-----'
ARMOR_END = '-----END AGE ENCRYPTED FILE-----'
