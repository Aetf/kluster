"""One repository and the hygiene resources that must come with it.

`ManagedRepository` is one repository as a component: the repository itself,
the vulnerability alerts, the branch protection where the plan offers it, the
labels and the variables the workflows read, and one Environment per census
entry. The stack
program is then wiring, and nothing parents a resource by hand.

It is the same shape as `dns.zone.ManagedZone` and named the same way for the
same reason: a component owning one upstream object plus the resources that
must come with it and are invisible until they are needed. What differs
between this installation's two repositories is census fields and parameters
-- visibility, the Environments, whether required checks are named -- and not
branches in the body.

**The census decides what exists; the parameters carry what GitHub stores.**
Which Environments a repository has, which of them a reviewer gates, which
labels and variables its workflows read and whether one of them merges with no
human are `conventions.forge`, and a label's meaning
travels beside its name there rather than arriving here, because a switch
whose name is declared and whose meaning is not is one a reader has to
reconstruct from the workflow that reads it. What arrives as a parameter is
what defines no entry in the design and only this stack reads: the
description and the required check names (framework/github.md §3).

**The posture is written refusing, and `ALLOWED` is every exception.** Each
setting that decides what a person or a workflow may do to a repository past
its gates -- a merge method, an admin past `main`'s protection or past an
Environment's reviewer, a force push, a deletion, a merge commit in `main`'s
history, a merge of a stale branch, a reviewer approving their own deployment
-- is refused unless `ALLOWED` names it, and an entry there carries its
reason. So loosening the posture is one line in that table with the reason
on it, rather than one value traded for another at a resource's argument.
The one loosening a repository asks for alone, unattended merging, is its
census row's (`Repository.unattended_merges`), with the reason on that row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum, auto
from typing import final

import pulumi
import pulumi_github as github

from kluster import conventions
from putils import Component

__all__ = ('ALLOWED', 'LABEL_COLOR', 'Allowance', 'ManagedRepository', 'UnenforceableChecks')

#: One color for every declared label. These are switches a workflow reads
#: rather than a taxonomy a reader browses, so a hue per label would be
#: meaning nobody put there; it is GitHub's own default-palette blue, which is
#: what the labels already carry.
LABEL_COLOR = 'BFD4F2'


@final
class Allowance(Enum):
    """A setting every repository refuses unless `ALLOWED` names it.

    Each member is the permissive side of one GitHub setting, and the
    component reads only whether `ALLOWED` names it: a member absent there is
    sent to GitHub as the refusal.
    """

    #: A pull request may land as its commits rebased onto `main`.
    REBASE_MERGES = auto()
    #: A pull request may land as one squashed commit.
    SQUASH_MERGES = auto()
    #: A pull request may land as a merge commit.
    MERGE_COMMITS = auto()
    #: An admin may merge or push past `main`'s protection (`enforce_admins`
    #: off).
    ADMINS_PAST_PROTECTION = auto()
    #: `main` takes force pushes.
    FORCE_PUSHES = auto()
    #: `main` may be deleted.
    DELETING_MAIN = auto()
    #: `main`'s history may hold merge commits (`required_linear_history`
    #: off).
    NONLINEAR_HISTORY = auto()
    #: A pull request may merge with its required checks green on a branch
    #: behind `main` (`strict` off).
    STALE_MERGES = auto()
    #: An admin may deploy into a gated Environment without its reviewer.
    ADMINS_PAST_GATES = auto()
    #: The author of a change may approve its deployment into a gated
    #: Environment.
    SELF_REVIEW = auto()


#: Every exception to the refusing posture, each beside its reason. A setting
#: named here is the permissive one on every repository of this installation,
#: and one absent is refused, so this table is the whole of what the posture
#: allows.
ALLOWED: Mapping[Allowance, str] = {
    Allowance.REBASE_MERGES: (
        'a pull request has to land somehow, and a rebase is the one method that keeps both its authorship and '
        "`main`'s linear history: a squash rewrites authorship to the merging identity, which for an unattended "
        'merge is `noreply@github.com`, and a merge commit is the history `NONLINEAR_HISTORY` refuses'
    ),
    Allowance.SELF_REVIEW: (
        'the installation has one operator, who opens every change and is the only reviewer a gated Environment '
        'has, so self-review is the only review there can be: refused, it would make the gate impassable rather '
        'than stricter'
    ),
}


class UnenforceableChecks(ValueError):
    """Required checks named on a repository whose plan offers no branch protection to enforce them."""

    def __init__(self, message: str, *, repository: str) -> None:
        super().__init__(message)
        self.repository: str = repository


class ManagedRepository(Component, pulumi_type='kluster:forge:ManagedRepository'):
    """One repository of this installation, with everything that hangs off it."""

    def __init__(
        self,
        name: str,
        *,
        entry: conventions.forge.Repository,
        description: str,
        required_checks: Sequence[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """
        :param entry: The census row this repository is declared from.
        :param description: What GitHub shows under the repository name.
        :param required_checks: The checks a pull request must pass before it
            may merge. Naming any is what asks for branch protection at all,
            and a repository that asks for none is handed the empty roll
            explicitly rather than by default: the protection is declared
            when the roll is non-empty and deleted when it is, so a default
            would let a call site that lost the argument take the guard off
            `main` with the component raising nothing — only the stack's own
            tests would notice.
        """
        # The `github` stack's state holds every repository under the type
        # below rather than this class's own: the alias moves each onto this
        # one at the next `up` with nothing replaced, and the engine carries it
        # down to every resource beneath (style/pulumi.md, a type token is
        # chosen). It goes once that state carries this class's type.
        aliases = pulumi.ResourceOptions(aliases=[pulumi.Alias(type_='kluster:components:forge:ManagedRepository')])
        super().__init__(name, opts=pulumi.ResourceOptions.merge(opts, aliases))
        self.entry = entry

        if required_checks and not entry.plan_offers_public_features:
            # Branch protection is public-repository-or-paid on this account
            # (framework/github.md §2), so declaring checks that can never be
            # enforced would leave a repository looking guarded and not be.
            raise UnenforceableChecks(
                f"{entry.name}: required checks are named, but this account's plan offers no branch "
                f'protection on a private repository',
                repository=entry.name,
            )

        self.repository = github.Repository(
            name,
            name=entry.name,
            description=description,
            visibility='public' if entry.public else 'private',
            has_issues=True,
            has_projects=False,
            has_wiki=False,
            allow_rebase_merge=Allowance.REBASE_MERGES in ALLOWED,
            allow_squash_merge=Allowance.SQUASH_MERGES in ALLOWED,
            allow_merge_commit=Allowance.MERGE_COMMITS in ALLOWED,
            # The two settings an unattended merge needs, offered only where
            # the census row says which workflow merges unattended and why. A
            # repository whose merges are all a person's is sent neither, which
            # Pulumi reads as unset.
            allow_auto_merge=True if entry.unattended_merges is not None else None,
            allow_update_branch=True if entry.unattended_merges is not None else None,
            delete_branch_on_merge=True,
            security_and_analysis=_secret_scanning(entry),
            # A `pulumi destroy` of this stack must not be able to delete the
            # repository that contains the stack.
            archive_on_destroy=True,
            opts=self.child_opts(protect=True),
        )

        #: Everything below hangs off the upstream object rather than off the
        #: component, which is what these resources are -- properties of a
        #: repository.
        on_repository = pulumi.ResourceOptions(parent=self.repository)

        # Its own resource rather than the `Repository` field of the same
        # name, which the provider deprecated in favor of exactly this.
        self.alerts = github.RepositoryVulnerabilityAlerts(
            name,
            repository=self.repository.name,
            enabled=True,
            opts=on_repository,
        )

        # A label a workflow branches on is a resource, because a workflow
        # that reads a label nothing declares fails in the quietest way there
        # is: the condition is simply never true, the behavior it guards is
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

        # A variable a workflow reads is a resource for the same reason a
        # label is. The value is public and comes from the census row, so
        # nothing here is a secret: a value that would have to be one is a
        # register row pushed by `credentials derived sync`, never a variable.
        self.variables = {
            variable.name: github.ActionsVariable(
                f'{name}-{variable.name}',
                repository=self.repository.name,
                variable_name=variable.name,
                value=variable.value,
                opts=on_repository,
            )
            for variable in entry.variables
        }

        # `strict` is "the branch must be up to date", without which a green
        # check describes code that was never combined with what is on `main`
        # -- which is exactly what the zero-diff proof claims about.
        self.protection = (
            github.BranchProtection(
                f'{name}-main',
                repository_id=self.repository.node_id,
                pattern='main',
                required_status_checks=[
                    {'strict': Allowance.STALE_MERGES not in ALLOWED, 'contexts': list(required_checks)}
                ],
                required_linear_history=Allowance.NONLINEAR_HISTORY not in ALLOWED,
                allows_deletions=Allowance.DELETING_MAIN in ALLOWED,
                allows_force_pushes=Allowance.FORCE_PUSHES in ALLOWED,
                # Enforced on the account owner too, while `ALLOWED` lets no
                # admin past. A gate the one person who can open it routinely
                # walks around is a suggestion, and this one is why a merge to
                # `main` implies `required_checks` were green on an up-to-date
                # branch. That is the whole of what a merge implies: a check
                # this list does not name is advisory however red it is, so
                # what those names are is a decision in its own right
                # (framework/github.md §3).
                enforce_admins=Allowance.ADMINS_PAST_PROTECTION not in ALLOWED,
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
                f'{name}-{environment.name}',
                repository=self.repository.name,
                environment=environment.name,
                deployment_branch_policy=(
                    {'protected_branches': True, 'custom_branch_policies': False}
                    if environment.branches is conventions.forge.BranchPolicy.PROTECTED_ONLY
                    else None
                ),
                reviewers=_reviewers(environment),
                # Both settings only qualify a reviewer, so they are sent
                # beside one and nowhere else: Pulumi drops a `None` input,
                # which is how an ungated Environment stays a bare one.
                prevent_self_review=(Allowance.SELF_REVIEW not in ALLOWED) if environment.gated else None,
                can_admins_bypass=(Allowance.ADMINS_PAST_GATES in ALLOWED) if environment.gated else None,
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
