"""The `github` stack: the forge itself — repositories, environments, gates.

Everything CI runs inside is configuration too, and until now it existed only
as console state: two repositories (`kluster`, `kluster-ops`), the per-stack
Environments that partition the credentials (ci.md §3), which of them a
reviewer gates, the branch protection that makes the zero-diff proof
load-bearing, and the two single-purpose GitHub Apps. None of it was written
down, so none of it could drift-check, review, or be rebuilt.

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
"""

from __future__ import annotations

import pulumi
import pulumi_github as github

#: The account both repositories live under. A fact of this installation,
#: like the names in `conventions`: this stack is not parameterized for
#: another owner.
OWNER = 'Aetf'

DEPLOYMENT_REPO = 'kluster'
OPS_REPO = 'kluster-ops'

#: Required before anything merges to `main`. Both run on every pull request
#: regardless of paths, which is what a required check has to do -- one that
#: only sometimes runs is one that blocks a pull request forever.
#:
#: The `preview` matrix is deliberately not here. Its check names carry the
#: stack (`preview (dns)`), so pinning them would freeze the stack list into a
#: setting that no longer moves with the code; and its verdict is consumed by
#: noop-automerge, which is where "the preview was empty" is decided.
REQUIRED_CHECKS = ('checks', 'changes')

#: One Environment per deployment layer (ci.md §3), so a job holds its layer's
#: credentials and no other. `physical` is split in two: the plan half is
#: ungated because reading the diff *is* the approval moment, and the apply
#: half is what a reviewer stands in front of.
PREVIEWED_LAYERS = ('dns', 'k8s-base', 'apps')
PLAN_ENVIRONMENT = 'physical-plan'
APPLY_ENVIRONMENT = 'physical'

#: The ops repository's own Environment, which is where the unattended drills
#: hold their credentials (credentials.md §3). Named here rather than spelled
#: at its resource, because it is the ops repository's half of the same
#: partition the constants above declare -- and the register's map is held
#: against it by name (`credentials/slots.py`).
DRILL_ENVIRONMENT = 'drill'


async def main() -> None:
    operator = github.get_user(username=OWNER)

    deployment = github.Repository(
        DEPLOYMENT_REPO,
        name=DEPLOYMENT_REPO,
        description='Pulumi Python for a Talos/Cilium cluster spanning OCI and a homelab LAN',
        visibility='public',
        has_issues=True,
        has_projects=False,
        has_wiki=False,
        # Rebase only. A squash rewrites authorship to the merging identity,
        # which for an unattended merge is `noreply@github.com`; a merge commit
        # would defeat the linear history the branch protection below asks for.
        allow_rebase_merge=True,
        allow_squash_merge=False,
        allow_merge_commit=False,
        allow_auto_merge=True,
        allow_update_branch=True,
        delete_branch_on_merge=True,
        security_and_analysis={
            'secret_scanning': {'status': 'enabled'},
            'secret_scanning_push_protection': {'status': 'enabled'},
        },
        # A `pulumi destroy` of this stack must not be able to delete the
        # repository that contains the stack.
        archive_on_destroy=True,
        opts=pulumi.ResourceOptions(protect=True),
    )

    # No `security_and_analysis`: secret scanning is a public-repository (or
    # paid) feature, and this repository is private on purpose -- it holds the
    # alert issues and every scheduled workflow (architecture.md).
    ops = github.Repository(
        OPS_REPO,
        name=OPS_REPO,
        description='Operations for the kluster installation: alert issues, drills, scheduled workflows',
        visibility='private',
        has_issues=True,
        has_projects=False,
        has_wiki=False,
        allow_rebase_merge=True,
        allow_squash_merge=False,
        allow_merge_commit=False,
        delete_branch_on_merge=True,
        archive_on_destroy=True,
        opts=pulumi.ResourceOptions(protect=True),
    )

    # Its own resource rather than the `Repository` field of the same name,
    # which the provider deprecated in favour of exactly this.
    for name, repository in ((DEPLOYMENT_REPO, deployment), (OPS_REPO, ops)):
        _ = github.RepositoryVulnerabilityAlerts(
            name,
            repository=repository.name,
            enabled=True,
            opts=pulumi.ResourceOptions(parent=repository),
        )

    # What the public flip bought (github.md §2). `strict` is "the branch must
    # be up to date", without which a green check describes code that was never
    # combined with what is on `main` -- which is exactly what the zero-diff
    # proof claims about.
    _ = github.BranchProtection(
        'main',
        repository_id=deployment.node_id,
        pattern='main',
        required_status_checks=[{'strict': True, 'contexts': list(REQUIRED_CHECKS)}],
        required_linear_history=True,
        allows_deletions=False,
        allows_force_pushes=False,
        # Including the account owner. A gate the one person who can open it
        # routinely walks around is a suggestion, and this one is load-bearing:
        # it is the reason a merge to `main` implies a passing preview.
        enforce_admins=True,
        opts=pulumi.ResourceOptions(parent=deployment),
    )

    # The layers CI previews from a pull request branch. No deployment branch
    # policy: `preview.yml` runs these Environments on the PR's own branch, and
    # restricting them to protected branches would fail every preview.
    for layer in PREVIEWED_LAYERS:
        _ = github.RepositoryEnvironment(
            layer,
            repository=deployment.name,
            environment=layer,
            opts=pulumi.ResourceOptions(parent=deployment),
        )

    # Main-only, both of them: the physical credentials can root the gateway,
    # so they are never handed to a pull request's code (ci.md §3).
    _ = github.RepositoryEnvironment(
        PLAN_ENVIRONMENT,
        repository=deployment.name,
        environment=PLAN_ENVIRONMENT,
        deployment_branch_policy={'protected_branches': True, 'custom_branch_policies': False},
        opts=pulumi.ResourceOptions(parent=deployment),
    )
    _ = github.RepositoryEnvironment(
        APPLY_ENVIRONMENT,
        repository=deployment.name,
        environment=APPLY_ENVIRONMENT,
        deployment_branch_policy={'protected_branches': True, 'custom_branch_policies': False},
        reviewers=[{'users': [int(operator.id)]}],
        # The installation has one operator, so the reviewer is the person who
        # opened the change; self-review is the only review there can be. Admin
        # bypass is off for the same reason enforce_admins is on above -- a door
        # with a key under the mat.
        prevent_self_review=False,
        can_admins_bypass=False,
        opts=pulumi.ResourceOptions(parent=deployment),
    )

    # Ungated by design: the drill's scope is its own gate (credentials.md §4),
    # and the ops repository is private, so branch protection is not available
    # to it on this plan anyway (github.md §2).
    _ = github.RepositoryEnvironment(
        DRILL_ENVIRONMENT,
        repository=ops.name,
        environment=DRILL_ENVIRONMENT,
        opts=pulumi.ResourceOptions(parent=ops),
    )

    pulumi.export('deployment_repository', deployment.full_name)
    pulumi.export('ops_repository', ops.full_name)
