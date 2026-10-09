"""The aliases both resolvers answer: a short name, and the device-plane name it stands for.

Data in the `dns` program's own area, since nothing else reads it, and rows of
the rule list (`rewrites.user_rules`) like every other answer an instance
gives: each is a CNAME rewrite, whose target the instance resolves through the
gateway's resolver, so an alias follows its device across a new lease without
an edit here. A target is a name on the device plane, which `Rewrite` holds at
construction, and one no rule answers, which `test_dns_stack` holds.
"""

from __future__ import annotations

from kluster.components.dns.rewrites import Rewrite
from kluster.conventions.homelab import HOMELAB_HOST_NAME

__all__ = ('ALIASES',)

ALIASES: tuple[Rewrite, ...] = (
    # The NAS is the homelab host; typed bare, and with the device plane's
    # search domain appended.
    Rewrite(domain='nas', answer=HOMELAB_HOST_NAME),
    Rewrite(domain='nas.home.arpa', answer=HOMELAB_HOST_NAME),
    # The television, under the name its remote's app was set up with.
    Rewrite(domain='s95d.iot.home.arpa', answer='samsung.iot.home.arpa'),
)
