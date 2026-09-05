"""The `github` stack: the forge itself — repositories, environments, gates.

Everything CI runs inside is configuration too, and it is declared here rather
than left as console state: two repositories (`kluster`, `kluster-ops`), the
per-stack Environments that partition the credentials (ci.md §3), which of them
a reviewer gates, and the branch protection that makes the zero-diff proof
load-bearing. Declared, it can be drift-checked, reviewed and rebuilt.

**Applied from the operator's machine, never from CI.** The credential this
stack needs can change branch protection and environment gates -- that is,
it can switch off the things that guard `main`. Handing it to a workflow
would mean anything that merges to `main` can also unguard `main`, which
undoes the partition ci.md §3 exists to create. The trade is cheap: the
forge changes a few times a year, while the credential would sit in CI
permanently. CI may still *preview* this stack to detect drift; it may not
apply it.

The Apps themselves are console-created (their private keys are §3 rows,
escrowed rather than held in the seed kit — credentials.md), and their
*installations* stay console state as well: the API that manages them takes
no personal access token, only a user-to-server token from an App's own OAuth
flow (kluster-ops#11). What is declared here is the repository state around
them.

**Which repositories and Environments there are is not decided here.** The
`credentials` command reads the same table, so it lives in `conventions.forge`
(github.md §3); this program declares from it and adds what only it decides --
the descriptions, the merge-strategy flags, and the checks a pull request must
pass. Each repository is one `ManagedRepository`, so this program is wiring:
the provider, the two entries, and the parameters that are neither.
"""

from __future__ import annotations

import os

import pulumi
import pulumi_github as github

from kluster import conventions
from kluster.components.forge import ManagedRepository

#: Required before anything merges to `main`. Both run on every pull request
#: regardless of paths, which is what a required check has to do -- one that
#: only sometimes runs is one that blocks a pull request forever.
#:
#: The `preview` matrix is deliberately not here. Its check names carry the
#: stack (`preview (dns)`), so pinning them would freeze the stack list into a
#: setting that no longer moves with the code; and its verdict is consumed by
#: noop-automerge, which is where "the preview was empty" is decided.
REQUIRED_CHECKS = ('checks', 'changes')

#: Where the provider's credential is read from. It is an account root held in
#: the personal estate, materialized into the environment by `mise.toml` out of
#: the operator machine's token file (github.md §1) -- deliberately *not* stack
#: configuration, because its absence is what stops this stack from being
#: applied by accident, and an escrowed copy would remove that.
TOKEN_VARIABLE = 'GITHUB_TOKEN'


async def main() -> None:
    # One provider for both repositories: they are two trees declared against
    # one account, which is what a stack program owns rather than a component.
    # The token is read here, at the line that builds the provider it opens,
    # and nowhere else.
    provider = github.Provider(
        f'{conventions.CLUSTER_NAME}-github',
        owner=conventions.forge.ACCOUNT.login,
        token=_token(),
    )
    on_github = pulumi.ResourceOptions(providers=[provider])

    deployment = ManagedRepository(
        conventions.forge.DEPLOYMENT.name,
        entry=conventions.forge.DEPLOYMENT,
        description='Pulumi Python for a Talos/Cilium cluster spanning OCI and a homelab LAN',
        required_checks=REQUIRED_CHECKS,
        # noop-automerge merges dependency bumps without a human, and this is
        # the repository it merges them into.
        unattended_merges=True,
        opts=on_github,
    )

    ops = ManagedRepository(
        conventions.forge.OPS.name,
        entry=conventions.forge.OPS,
        description='Operations for the kluster installation: alert issues, drills, scheduled workflows',
        opts=on_github,
    )

    pulumi.export('deployment_repository', deployment.repository.full_name)
    pulumi.export('ops_repository', ops.repository.full_name)


def _token() -> str:
    """The account-root token, out of the environment.

    Read here rather than left to the SDK, which is not only style:
    `pulumi_github` falls back to this same variable and, failing that, runs
    **anonymously**, so a missing token is not a refusal but a run that
    authenticates as nobody and fails partway through on the first write.
    Refusing by name turns that into a stop before anything is declared.

    Handed over in the clear. What keeps an account root out of state is the
    generated provider, which marks this input secret itself; a second
    wrapping here would read as the mechanism without being it, and would
    leave the case that pins the property passing over a provider release that
    had stopped applying it.
    """
    token = os.environ.get(TOKEN_VARIABLE)
    if not token:
        raise ValueError(
            f'{TOKEN_VARIABLE} is unset, so this run would authenticate as nobody and fail on its first '
            f'write. The `github` stack is applied from the operator machine with an account-root token '
            f'that `credentials root github remember` puts in place (framework/github.md §1).'
        )
    return token
