"""The `state-backend` stack: the appliance whose Postgres keeps every other stack's state.

An operator stack whose state is committed (framework/pulumi.md §3.3): the
backend it declares cannot hold its own state, so the checkpoint lives under
`checkpoints/` in this repository and a run of it goes through the
`operator-stack` driver alone. Its configuration is filled by the
`credentials derived` rows the appliance runs on (credentials.md §3); the
program that declares the appliance from it is rfc-006 §4, and until it is
written the box is still built by `state-backend provision`
(physical/state-backend.md).
"""

from __future__ import annotations


async def main() -> None:
    raise NotImplementedError('state-backend stack: see docs/rfc/rfc-006-state-backend-stack.md §4')
