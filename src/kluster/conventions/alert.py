"""The alert payload: what every post to the ops repository's dispatch intake carries.

An alert raised outside the cluster travels as a `repository_dispatch` into the
ops repository (cluster/architecture.md §4.3): the event type below, and a
client payload whose keys are `FIELDS`. Two sides spell it. The posting side
is, in this repository, the one producer `.github/workflows/alert.yml`, which
every workflow raising an alert calls rather than posting for itself, and in
the ops repository, that repository's own workflows. The receiving side is the
ops repository's dispatch handler. Neither ops-repository side is built;
operations.md §4 says what of the contract is.

No program in this tree imports it: both sides are workflows, which no import
reaches. It lives here for the reason `conventions.forge`'s logins do -- a
workflow spells it as literals, and a test holding those literals to one
spelling is the only thing that keeps them from drifting. This repository's
side is held in `tests/test_conventions.py`; the handler's is the ops
repository's to hold, since nothing here can read it.

Read qualified -- `conventions.alert.EVENT`, `conventions.alert.Tier` --
because the names are common nouns that mean this one thing only while the
alert stands beside them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import final

#: The `event_type` of the dispatch, and the one entry of the handler's
#: `types:` filter. A producer that spells it differently posts a dispatch
#: GitHub accepts with a `204` and the handler never sees, so it is spelled
#: here and every other spelling is held to it.
EVENT = 'kluster-alert'


@final
class Tier(StrEnum):
    """How an alert is delivered, which is decided by what it asks of a human.

    The payload carries the tier and the handler alone turns it into delivery
    (cluster/architecture.md §4.3): a producer says what kind of alert it is
    raising and never how to deliver it.
    """

    #: Informational and self-resolving: a phone push and nothing else.
    NOTIFY = 'notify'
    #: Has a playbook and needs a human: a phone push and an issue in the ops
    #: repository, one open issue per `key`, so a repeat is a comment on the
    #: issue already open rather than a second push.
    ACTIONABLE = 'actionable'
    #: A check-in with neither push nor issue: it restarts the source's
    #: timer on the Home Assistant side, which is how a scheduled workflow
    #: that stops running is noticed (operations.md §4).
    HEARTBEAT = 'heartbeat'


#: The keys of the client payload, every one of them sent on every alert.
#:
#: - `source`: the workflow that raised it, as `<repository>/<workflow>`, where
#:   `<workflow>` is the workflow's `name:`, which is its file name without the
#:   suffix -- `kluster/drift`, `kluster-ops/probes`. The producer reads the
#:   name from the run, so a test holds every caller's `name:` to its file.
#: - `tier`: a `Tier`.
#: - `key`: the alert's identity for deduplication, which is the `source`
#:   unless the workflow raises several distinct alerts and names each.
#: - `summary`: one line, static, authored with the alert.
#: - `playbook`: where the response procedure is written, as a document and
#:   a section -- `docs/framework/ci.md §3.1` -- which the handler renders as a
#:   link. An alert without one is not shipped (cluster/architecture.md §4.3).
#: - `run`: the URL of the run that raised it.
#: - `details`: free text, possibly empty -- a commit subject, a probe's
#:   verdicts. Never a credential, a token or a secret-bearing URL: the issue
#:   it lands in is private, and that is the whole of the content rule.
FIELDS = ('source', 'tier', 'key', 'summary', 'playbook', 'run', 'details')

#: The fields a producer fills in from the run itself, so that the workflow
#: raising an alert never passes them: where it came from and which run it
#: was are facts GitHub already knows, and a caller that typed them could
#: only get them wrong.
COMPUTED = ('source', 'run')
