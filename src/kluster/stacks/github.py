"""The `github` stack: the forge itself — repositories, environments, gates.

Everything CI runs inside is configuration too, and it is declared here rather
than left as console state: two repositories (`kluster`, `kluster-ops`), the
per-stack Environments that partition the credentials (ci.md §3), which of them
a reviewer gates, and the branch protection that makes the zero-diff proof
load-bearing. Declared, it can be drift-checked, reviewed and rebuilt.

**Applied from the operator's machine, never from CI.** The credential this
stack needs can change branch protection and environment gates -- that is,
it can switch off the things that guard `main`. Handing that to a workflow
would mean anything that merges to `main` can also unguard `main`, which
undoes the partition ci.md §3 exists to create. What holds the line is that
no workflow names this stack at all, drift detection included (github.md §1);
the trade is cheap, because the forge changes a few times a year while a
workflow that ran this would sit in CI permanently.

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
#: setting that no longer moves with the code. What leaving it out causes is
#: that a red `preview` blocks nothing: this tuple is the whole of what a merge
#: to `main` is gated on, and the unattended merge proves a zero diff of its own
#: rather than reading this matrix (github.md §3).
REQUIRED_CHECKS = ('checks', 'changes')

#: Where the provider's credential is read: this stack's own committed
#: configuration, at the line that builds the provider it opens and nowhere
#: else (rfc-002 §8.1). Bare, and therefore in this project's namespace rather
#: than the provider package's, for the reason the zones token's key is
#: (`stacks/dns.py`): a `github:` entry in a committed stack file is
#: indistinguishable from the ambient configuration this repository has removed
#: everywhere else.
#:
#: The token is hand-made in the GitHub UI and this repository mints no
#: successor for it, which decides how it gets here but not where it lives:
#: `credentials derived github-admin record` takes it from that console into
#: this file, the same delivery every other provider credential of this
#: installation has (credentials.md §3).
ADMIN_TOKEN = 'githubAdminToken'


async def main() -> None:
    config = pulumi.Config()

    # One provider for both repositories: they are two trees declared against
    # one account, which is what a stack program owns rather than a component.
    # The token is read here, at the line that builds the provider it opens,
    # and nowhere else.
    provider = github.Provider(
        f'{conventions.CLUSTER_NAME}-github',
        owner=conventions.forge.ACCOUNT.login,
        token=_token(config),
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
        # No branch protection: the plan offers none on a private repository
        # (github.md §2), and the component refuses a non-empty roll here. The
        # empty roll is passed rather than defaulted so that the decision
        # reads beside the repository it is about.
        required_checks=(),
        opts=on_github,
    )

    pulumi.export('deployment_repository', deployment.repository.full_name)
    pulumi.export('ops_repository', ops.repository.full_name)


def _token(config: pulumi.Config) -> pulumi.Output[str]:
    """The admin token, out of this stack's committed configuration.

    Required rather than left to the SDK, which is not only style:
    `pulumi_github` falls back to a `GITHUB_TOKEN` in the environment and,
    failing that, runs **anonymously**, so an unconfigured stack is not a
    refusal but a run that authenticates as nobody and fails partway through on
    the first write. A missing key stops the run before anything is declared.

    The refusal names the command that fills the key, because the state it is
    most likely to meet is a checkout whose `Pulumi.github.yaml` predates this
    key: Pulumi's own message says to run `pulumi config set`, which would put
    the credential in the process table and skip the read-back every other
    config slot is delivered with.
    """
    try:
        return config.require_secret(ADMIN_TOKEN)
    except pulumi.ConfigMissingError as exc:
        raise ValueError(
            f"{ADMIN_TOKEN} is not in this stack's configuration, so this run would authenticate as nobody "
            f'and fail on its first write. The `github` stack is applied from the operator machine with an '
            f'admin token that `credentials derived github-admin record` takes from the GitHub UI into '
            f'Pulumi.github.yaml (credentials.md §3).'
        ) from exc
