"""The opening of a stand-in for `mise` that a case runs `checks.yml`'s prose step against.

The step writes the checker's configuration with `mise x uv -- uv run
ltex-config <output>` before it runs the checker. A stand-in that starts
with `HEAD` runs the real script for that invocation, from the suite's own
interpreter, and goes on to the text after it for every other one, which is
where each module's stand-in for the checker lives.
"""

from __future__ import annotations

import sys

#: The stand-in's first lines: its interpreter, and the `ltex-config` arm.
HEAD = f"""#!{sys.executable}
import sys
if 'ltex-config' in sys.argv:
    from kluster.scripts import ltex_config
    sys.exit(ltex_config.main(sys.argv[sys.argv.index('ltex-config') + 1 :]))
"""
