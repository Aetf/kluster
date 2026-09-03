"""The forge: the account, its repositories, and the Environments CI deploys from.

Two programs must agree on this table. The `github` stack declares the
repositories and their Environments; the `credentials` command pushes a secret
into every Environment a register row names (credentials.md §3). A script may
import `conventions` and `lib` and nothing a stack declares from (AGENTS.md's
import contract), so `conventions` is the only home the two share.

**The row is the unit, and it carries what defines the entry in the design.**
ci.md §3 defines the credential partition in exactly the terms below — which
branches may deploy into a cell, and whether a reviewer stands in front of it —
and the labels a workflow branches on are the same kind of fact about what a
repository *is* here. What stays in the `github` stack is the upstream object's
own settings, which are no part of any entry: the required check names, the
repository descriptions, the merge-strategy flags (framework/github.md §3).

Read qualified — `conventions.forge.DEPLOYMENT` — because the names below are
common nouns that mean one particular thing only while the forge stands beside
them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import final


@final
@dataclass(frozen=True)
class Account:
    """The account the repositories live under, under both of the names it answers to.

    A login and a user id are one account spelled two ways: the login is what a
    URL and an `owner/name` carry, and the id is what the API's reviewer and
    assignee lists take. They are bound rather than held flat because using one
    without the other is how the two come to disagree.
    """

    #: As a URL spells it.
    login: str
    #: As the API's member lists spell it. Minted once and never changed, so it
    #: is an identity rather than a measurement — the ruling that put the
    #: overlay node identifiers in the roster — and recording it is what lets a
    #: program name the account without an invoke to resolve the login.
    user_id: int


#: A fact of this installation, like the names elsewhere in `conventions`:
#: nothing here is parameterized for another account.
ACCOUNT = Account(login='Aetf', user_id=1519759)


@final
class BranchPolicy(Enum):
    """Which branches a deployment into an Environment may run from."""

    #: Any branch, a pull request's own included.
    ANY_BRANCH = 'any'
    #: Protected branches only, which on these repositories means `main`.
    PROTECTED_ONLY = 'protected'


@final
@dataclass(frozen=True)
class Environment:
    """One cell of ci.md §3's credential partition.

    A job holds its Environment's credentials and no other, so which
    Environments exist is the shape of that partition rather than a setting of
    the repository they sit in.
    """

    name: str
    branches: BranchPolicy
    #: Whether a reviewer stands in front of a deployment into it. *Who* that
    #: reviewer is is the declaring stack's decision; the entry says only that
    #: the cell is gated.
    gated: bool = False


@final
@dataclass(frozen=True)
class Repository:
    """One repository as this installation defines it, rather than as GitHub stores it.

    What it is, the cells of the credential partition it carries, and the
    labels its workflows branch on. Its settings are not here: they define no
    entry and only the `github` stack reads them.
    """

    name: str
    #: Public repositories are the ones this account's plan gives the features
    #: `plan_offers_public_features` answers for.
    public: bool
    #: In the order the merge chain runs them, which is a fact the credentials
    #: command reads back out.
    environments: tuple[Environment, ...]
    #: The labels a workflow branches on. A workflow that reads a label nothing
    #: declares fails in the quietest way there is — the condition is simply
    #: never true — so the set is written down here rather than made by hand in
    #: a console.
    labels: tuple[str, ...] = ()

    @property
    def full_name(self) -> str:
        """`owner/name`, which is GitHub's own word for it and what every API takes."""
        return f'{ACCOUNT.login}/{self.name}'

    @property
    def plan_offers_public_features(self) -> bool:
        """Whether branch protection, environment gates and secret scanning are available.

        All three are public-repository-or-paid on this account
        (framework/github.md §2), so asking for one on a private repository is
        an API error rather than a stricter setting. Derived from visibility
        rather than declared beside it: an answer nobody can write out of step
        with the flag it follows from.
        """
        return self.public


#: The ops repository's only Environment, which carries the unattended drills'
#: credentials. Ungated because its scope is the gate (credentials.md §4).
#: Any branch, for want of an alternative: a private repository has no
#: protected branches to name on this plan (framework/github.md §2).
DRILL = Environment('drill', BranchPolicy.ANY_BRANCH)

#: The repository this stack is declared in, and the one CI deploys from.
DEPLOYMENT = Repository(
    name='kluster',
    public=True,
    environments=(
        # Protected branches only, both: these credentials can root the
        # gateway, so a pull request's own code never runs with them (ci.md
        # §3). `physical` is two cells holding the same credentials because
        # reading the plan's diff *is* the approval moment — so the plan half
        # is ungated, and the apply half is what a reviewer stands in front of.
        Environment('physical-plan', BranchPolicy.PROTECTED_ONLY),
        Environment('physical', BranchPolicy.PROTECTED_ONLY, gated=True),
        # `preview.yml` runs these three from a pull request's own branch, so a
        # protected-branches policy would fail every preview — the check the
        # merge chain rests on.
        Environment('dns', BranchPolicy.ANY_BRANCH),
        Environment('k8s-base', BranchPolicy.ANY_BRANCH),
        Environment('apps', BranchPolicy.ANY_BRANCH),
    ),
    # `noop-automerge.yml` branches on this one: it is the escape hatch that
    # opts a pull request out of the zero-diff proof (ci.md §3).
    labels=('expect-changes',),
)

#: The notification and drill repository. Private on purpose — it holds the
#: alert issues and every scheduled workflow — which is what puts branch
#: protection and secret scanning out of its reach.
OPS = Repository(
    name='kluster-ops',
    public=False,
    environments=(DRILL,),
)

#: Every repository the forge declares, which is every repository of this
#: installation: a name absent here is one nothing in this tree manages.
REPOSITORIES = (DEPLOYMENT, OPS)
