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

**Which repositories and Environments there are is not decided here.** That
table is read by the `credentials` command too, so it lives in
`conventions.forge` (github.md §3); this program declares from it and adds what
only it decides — the descriptions, the merge-strategy flags, and the checks a
pull request must pass.
"""

from __future__ import annotations

import pulumi
import pulumi_github as github

from kluster import conventions

#: Required before anything merges to `main`. Both run on every pull request
#: regardless of paths, which is what a required check has to do -- one that
#: only sometimes runs is one that blocks a pull request forever.
#:
#: The `preview` matrix is deliberately not here. Its check names carry the
#: stack (`preview (dns)`), so pinning them would freeze the stack list into a
#: setting that no longer moves with the code; and its verdict is consumed by
#: noop-automerge, which is where "the preview was empty" is decided.
REQUIRED_CHECKS = ('checks', 'changes')


def _visibility(repository: conventions.forge.Repository) -> str:
    """What the API calls the census's `public` flag."""
    return 'public' if repository.public else 'private'


def _secret_scanning(
    repository: conventions.forge.Repository,
) -> github.RepositorySecurityAndAnalysisArgsDict | None:
    """Secret scanning and its push protection, where the plan offers them.

    Both are public-repository-or-paid on this account (github.md §2), so
    asking for them where the plan does not offer them is an API error rather
    than a stricter setting. Asking follows the census's derived answer, which
    is what keeps this setting from being written out of step with a
    visibility that moved.
    """
    if not repository.plan_offers_public_features:
        return None
    return {
        'secret_scanning': {'status': 'enabled'},
        'secret_scanning_push_protection': {'status': 'enabled'},
    }


async def main() -> None:
    deployment = github.Repository(
        conventions.forge.DEPLOYMENT.name,
        name=conventions.forge.DEPLOYMENT.name,
        description='Pulumi Python for a Talos/Cilium cluster spanning OCI and a homelab LAN',
        visibility=_visibility(conventions.forge.DEPLOYMENT),
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
        security_and_analysis=_secret_scanning(conventions.forge.DEPLOYMENT),
        # A `pulumi destroy` of this stack must not be able to delete the
        # repository that contains the stack.
        archive_on_destroy=True,
        opts=pulumi.ResourceOptions(protect=True),
    )

    # Private on purpose -- it holds the alert issues and every scheduled
    # workflow (architecture.md) -- which is why the census answers no to the
    # plan's public-only features and this one asks for no secret scanning.
    ops = github.Repository(
        conventions.forge.OPS.name,
        name=conventions.forge.OPS.name,
        description='Operations for the kluster estate: alert issues, drills, scheduled workflows',
        visibility=_visibility(conventions.forge.OPS),
        has_issues=True,
        has_projects=False,
        has_wiki=False,
        allow_rebase_merge=True,
        allow_squash_merge=False,
        allow_merge_commit=False,
        delete_branch_on_merge=True,
        security_and_analysis=_secret_scanning(conventions.forge.OPS),
        archive_on_destroy=True,
        opts=pulumi.ResourceOptions(protect=True),
    )

    # Each census entry beside the repository declared from it.
    declared = ((conventions.forge.DEPLOYMENT, deployment), (conventions.forge.OPS, ops))

    # Its own resource rather than the `Repository` field of the same name,
    # which the provider deprecated in favour of exactly this.
    for entry, repository in declared:
        _ = github.RepositoryVulnerabilityAlerts(
            entry.name,
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

    # One Environment per census entry, under the repository that carries it.
    # An entry decides two things and the census says why for each: which
    # branches may deploy into it, and whether a reviewer stands in front of
    # it. Everything else about an Environment is the same in all of them.
    for entry, repository in declared:
        for environment in entry.environments:
            # A reviewer list is what makes an Environment a gate, so the two
            # settings that only qualify a reviewer are sent only beside one.
            # Pulumi drops an input that serializes to `None`, which is how an
            # ungated Environment stays a bare one.
            reviewers: list[github.RepositoryEnvironmentReviewerArgsDict] | None = (
                [{'users': [conventions.forge.OPERATOR_ID]}] if environment.gated else None
            )
            _ = github.RepositoryEnvironment(
                environment.name,
                repository=repository.name,
                environment=environment.name,
                deployment_branch_policy=(
                    {'protected_branches': True, 'custom_branch_policies': False}
                    if environment.branches is conventions.forge.BranchPolicy.PROTECTED_ONLY
                    else None
                ),
                reviewers=reviewers,
                # The estate has one operator, so the reviewer is the person
                # who opened the change; self-review is the only review there
                # can be. Admin bypass is off for the same reason
                # enforce_admins is on above -- a door with a key under the mat.
                prevent_self_review=False if reviewers else None,
                can_admins_bypass=False if reviewers else None,
                opts=pulumi.ResourceOptions(parent=repository),
            )

    pulumi.export('deployment_repository', deployment.full_name)
    pulumi.export('ops_repository', ops.full_name)
