"""One repository and the hygiene resources that must come with it.

`ManagedRepository` is one repository as a component: the repository itself,
the vulnerability alerts, the branch protection where the plan offers it, the
labels the workflows read, and one Environment per census entry. The stack
program is then wiring, and nothing parents a resource by hand.

It is the same shape as `dns.zone.ManagedZone` and named the same way for the
same reason: a component owning one upstream object plus the resources that
must come with it and are invisible until they are needed. What differs
between this installation's two repositories is census fields and parameters
-- visibility, the Environments, whether required checks are named -- and not
branches in the body.

**The census decides what exists; the parameters carry what GitHub stores.**
Which Environments a repository has, which of them a reviewer gates and which
labels its workflows read are `conventions.forge`, and a label's meaning
travels beside its name there rather than arriving here, because a switch
whose name is declared and whose meaning is not is one a reader has to
reconstruct from the workflow that reads it. What arrives as a parameter is
what defines no entry in the design and only this stack reads: the
description, the required check names, and the pull-request automation flags
(framework/github.md §3).
"""

from __future__ import annotations

from collections.abc import Sequence

import pulumi
import pulumi_github as github

from kluster import conventions
from putils import Component

__all__ = ('LABEL_COLOR', 'ManagedRepository')

#: One color for every declared label. These are switches a workflow reads
#: rather than a taxonomy a reader browses, so a hue per label would be
#: meaning nobody put there; it is GitHub's own default-palette blue, which is
#: what the labels already carry.
LABEL_COLOR = 'BFD4F2'


class ManagedRepository(Component):
    """One repository of this installation, with everything that hangs off it."""

    def __init__(
        self,
        name: str,
        *,
        entry: conventions.forge.Repository,
        description: str,
        required_checks: Sequence[str] = (),
        unattended_merges: bool = False,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """
        :param entry: The census row this repository is declared from.
        :param description: What GitHub shows under the repository name.
        :param required_checks: The checks a pull request must pass before it
            may merge. Naming any is what asks for branch protection at all.
        :param unattended_merges: Whether auto-merge and branch updating are
            offered. They are the two settings an unattended merge needs: one
            queues it behind the checks, the other lets a pull request satisfy
            "must be up to date" without a human. A repository whose pull
            requests are all merged by hand asks for neither.
        """
        super().__init__(name, opts=opts)
        self.entry = entry

        if required_checks and not entry.plan_offers_public_features:
            # Branch protection is public-repository-or-paid on this account
            # (framework/github.md §2), so declaring checks that can never be
            # enforced would leave a repository looking guarded and not be.
            raise ValueError(
                f"{entry.name}: required checks are named, but this account's plan offers no branch "
                f'protection on a private repository'
            )

        self.repository = github.Repository(
            name,
            name=entry.name,
            description=description,
            visibility='public' if entry.public else 'private',
            has_issues=True,
            has_projects=False,
            has_wiki=False,
            # Rebase only. A squash rewrites authorship to the merging
            # identity, which for an unattended merge is `noreply@github.com`;
            # a merge commit would defeat the linear history the branch
            # protection below asks for.
            allow_rebase_merge=True,
            allow_squash_merge=False,
            allow_merge_commit=False,
            allow_auto_merge=True if unattended_merges else None,
            allow_update_branch=True if unattended_merges else None,
            delete_branch_on_merge=True,
            security_and_analysis=_secret_scanning(entry),
            # A `pulumi destroy` of this stack must not be able to delete the
            # repository that contains the stack.
            archive_on_destroy=True,
            opts=self.child_opts(
                protect=True,
                # This repository was declared by the stack program before it
                # was declared by a component, so introducing this component
                # moved its URN one level down. The alias is what makes that a
                # rename rather than "create the parented one, delete the
                # unparented one" -- and the delete would be refused by the
                # `protect` above anyway. Every resource below is parented on
                # the repository rather than on the component, so each of them
                # inherits this alias and keeps its own URN too.
                aliases=[pulumi.Alias(parent=pulumi.ROOT_STACK_RESOURCE)],
            ),
        )

        #: Everything below hangs off the upstream object rather than off the
        #: component, which is both what these resources are -- properties of
        #: a repository -- and what makes one alias enough for all of them.
        on_repository = pulumi.ResourceOptions(parent=self.repository)

        # Its own resource rather than the `Repository` field of the same
        # name, which the provider deprecated in favour of exactly this.
        self.alerts = github.RepositoryVulnerabilityAlerts(
            name,
            repository=self.repository.name,
            enabled=True,
            opts=on_repository,
        )

        # A label a workflow branches on is a resource, because a workflow
        # that reads a label nothing declares fails in the quietest way there
        # is: the condition is simply never true, the behaviour it guards is
        # unavailable at the moment somebody needs it, and nothing reports it.
        self.labels = {
            label.name: github.IssueLabel(
                f'{name}-{label.name}',
                repository=self.repository.name,
                name=label.name,
                color=LABEL_COLOR,
                description=label.description,
                opts=on_repository,
            )
            for label in entry.labels
        }

        # `strict` is "the branch must be up to date", without which a green
        # check describes code that was never combined with what is on `main`
        # -- which is exactly what the zero-diff proof claims about.
        self.protection = (
            github.BranchProtection(
                'main',
                repository_id=self.repository.node_id,
                pattern='main',
                required_status_checks=[{'strict': True, 'contexts': list(required_checks)}],
                required_linear_history=True,
                allows_deletions=False,
                allows_force_pushes=False,
                # Including the account owner. A gate the one person who can
                # open it routinely walks around is a suggestion, and this one
                # is why a merge to `main` implies a passing preview.
                enforce_admins=True,
                opts=on_repository,
            )
            if required_checks
            else None
        )

        # One Environment per census entry. An entry decides two things and
        # the census says why for each: which branches may deploy into it, and
        # whether a reviewer stands in front of it. Everything else about an
        # Environment is the same in all of them.
        self.environments = {
            environment.name: github.RepositoryEnvironment(
                environment.name,
                repository=self.repository.name,
                environment=environment.name,
                deployment_branch_policy=(
                    {'protected_branches': True, 'custom_branch_policies': False}
                    if environment.branches is conventions.forge.BranchPolicy.PROTECTED_ONLY
                    else None
                ),
                reviewers=_reviewers(environment),
                # Admin bypass is off for the same reason `enforce_admins` is
                # on above -- a door with a key under the mat. Both settings
                # only qualify a reviewer, so they are sent beside one and
                # nowhere else: Pulumi drops a `None` input, which is how an
                # ungated Environment stays a bare one.
                prevent_self_review=False if environment.gated else None,
                can_admins_bypass=False if environment.gated else None,
                opts=on_repository,
            )
            for environment in entry.environments
        }

        self.register_outputs({'full_name': self.repository.full_name})


def _secret_scanning(
    entry: conventions.forge.Repository,
) -> github.RepositorySecurityAndAnalysisArgsDict | None:
    """Secret scanning and its push protection, on the repositories that can have them.

    Which those are, and why, is `Repository.plan_offers_public_features`:
    asking for it where the plan does not offer it is an API error rather than
    a stricter setting.
    """
    if not entry.plan_offers_public_features:
        return None
    return {
        'secret_scanning': {'status': 'enabled'},
        'secret_scanning_push_protection': {'status': 'enabled'},
    }


def _reviewers(
    environment: conventions.forge.Environment,
) -> list[github.RepositoryEnvironmentReviewerArgsDict] | None:
    """Who stands in front of a gated Environment, and nobody in front of the rest.

    The account owner is the reviewer: this installation has one operator, so
    the reviewer is whoever opened the change and self-review is the only
    review there can be. A reviewer list is also what makes an Environment a
    gate, so an ungated entry gets none rather than an empty one.
    """
    if not environment.gated:
        return None
    return [{'users': [conventions.forge.ACCOUNT.user_id]}]
