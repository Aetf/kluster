"""The forge: the account, its repositories, and the Environments CI deploys from.

Two programs must agree on this table. The `github` stack declares the
repositories and the Environments; the `credentials` command pushes a secret
into every Environment a register row names (credentials.md §3), and a script
may import `conventions` and `lib` and nothing else. `conventions` is therefore
the only home the two share, and a second copy held equal by a test is what
this replaces.

What is *not* here is what one program decides alone: the required check names,
the repository descriptions and the merge-strategy flags are the `github`
stack's own business, and no second reader has an opinion about them
(framework/github.md §3).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: The account both repositories live under, as a URL spells it. An estate
#: fact, like the names elsewhere in `conventions`: nothing here is
#: parameterized for another owner.
OWNER = 'Aetf'

#: The same account as the API's reviewer lists spell it. A user id is minted
#: once and never changes, which makes it an identity rather than a
#: measurement — the same ruling that put the overlay node ids in the roster —
#: so it is recorded here instead of resolved by an invoke on every run.
OPERATOR_ID = 1519759


class BranchPolicy(Enum):
    """Which branches a deployment into an Environment may run from."""

    #: A pull request's own branch may deploy. What `preview.yml` needs: a
    #: protected-branches policy on these would fail every preview.
    ANY_BRANCH = 'any'
    #: `main` and nothing else. For credentials a pull request's code must
    #: never run with (ci.md §3).
    PROTECTED_ONLY = 'protected'


@dataclass(frozen=True)
class Environment:
    """One deployment Environment, which is one layer's credential partition.

    A job holds its layer's credentials and no other (ci.md §3), so which
    Environments exist is the shape of that partition rather than a setting.
    """

    name: str
    branches: BranchPolicy
    #: A reviewer stands in front of the deployment. The estate has one
    #: operator, so this gate is a pause for a human to read a diff rather than
    #: a second pair of eyes.
    gated: bool = False


@dataclass(frozen=True)
class Repository:
    """One repository, and everything about it a second program has to know."""

    name: str
    #: Public repositories are the ones this account's plan gives the features
    #: below; see `plan_offers_public_features`.
    public: bool
    #: In the order the merge chain runs them, which is a fact the credentials
    #: command reads: a layer missing from the front of this tuple is a layer
    #: whose secrets arrive after the layer that needs them.
    environments: tuple[Environment, ...]
    #: The labels a workflow branches on — `noop-automerge.yml` reads
    #: `expect-changes`, the escape hatch that opts a pull request out of the
    #: zero-diff proof (ci.md §3). A workflow that reads a label nothing
    #: declares fails in the quietest way there is: the condition is never
    #: true, and the escape hatch is missing at the moment somebody needs it.
    #: So the set is written down rather than made by hand in a console.
    labels: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        """`owner/name`, which is how every API and every `gh` invocation spells it."""
        return f'{OWNER}/{self.name}'

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
#: credentials. Ungated because its scope is the gate (credentials.md §4), and
#: the repository is private, so this plan offers it no gate anyway.
DRILL = Environment('drill', BranchPolicy.ANY_BRANCH)

#: The repository this stack is declared in, and the one CI deploys from.
DEPLOYMENT = Repository(
    name='kluster',
    public=True,
    environments=(
        # `physical` is two Environments holding the same credentials: reading
        # the plan's diff *is* the approval moment, so the plan half is
        # ungated, and the apply half is what a reviewer stands in front of.
        Environment('physical-plan', BranchPolicy.PROTECTED_ONLY),
        Environment('physical', BranchPolicy.PROTECTED_ONLY, gated=True),
        Environment('dns', BranchPolicy.ANY_BRANCH),
        Environment('k8s-base', BranchPolicy.ANY_BRANCH),
        Environment('apps', BranchPolicy.ANY_BRANCH),
    ),
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

#: Every repository the forge stack declares.
REPOSITORIES = (DEPLOYMENT, OPS)
