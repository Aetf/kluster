"""The forge: the account, its repositories, and the Environments CI deploys from.

Two programs must agree on this table. The `github` stack declares the
repositories and their Environments; the `credentials` command pushes a secret
into every Environment a register row names (credentials.md §3). A script may
import `conventions` and `lib` and nothing a stack declares from (AGENTS.md's
import contract), so `conventions` is the only home the two share.

**The row is the unit, and it carries what defines the entry in the design.**
ci.md §3 defines the credential partition in exactly the terms below — which
branches may deploy into a cell, and whether a reviewer stands in front of it —
and the switches a workflow branches on are the same kind of fact about what a
repository *is* here: the labels it reads, the identities it tests the
author of a pull request against, the variables it reads, and the Apps whose
tokens may push to it. What stays in the `github` stack is the
upstream object's own settings, which are no part of any entry: the required
check names, the repository descriptions, the merge-strategy flags
(framework/github.md §3).

**`authors` creates nothing, and that is not what decides where it lives.**
style/pulumi.md places a census by counting the programs that read it,
"regardless of whether each declares a resource from the table" — "which
program turns the table into resources does not enter into it". This table has
the readers the first paragraph names, so the row belongs in `conventions`, and
a field of the row travels with it. What holds that field honest is elsewhere:
a workflow spells the login as a literal, and a test holds that literal to this
file. Without the entry the two drift with nothing to notice.

**The Apps are here rather than in `conventions.providers`** because they are
not accounts this program declares into: an App is an actor on the forge, its
installation is a fact about a repository row, and the one place its client
id reaches is a variable of that row. `providers` holds the identifiers of the
clouds the stacks push to; the forge's own identifiers -- the account, the
Apps -- sit with the forge.

Read qualified — `conventions.forge.DEPLOYMENT` — because the names below are
common nouns that mean one particular thing only while the forge stands beside
them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import final

from kluster.conventions import identity


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
class Label:
    """One issue label a workflow branches on.

    The name and what it means travel together, because a switch whose name is
    declared and whose meaning is not is one a reader has to reconstruct from
    the workflow that reads it. What it looks like is not here: every declared
    label carries the same color, and nothing outside the component that
    declares them has an opinion about which, so that is the component's own.
    """

    name: str
    #: What GitHub shows beside the name, for whoever is deciding whether to
    #: reach for it.
    description: str


@final
@dataclass(frozen=True)
class Author:
    """One account a workflow of this repository tests the author of a pull request against.

    **A login and no user id, which is the opposite of `Account` above, and the
    difference is what each is for.** `Account` is named to GitHub — its
    reviewer and assignee lists take the id — so holding one spelling without
    the other is how the two come to disagree. An author here is named to
    nothing: no call in this tree passes it, and the only thing that reads it
    is a workflow comparing `…user.login` to a string. Recording an id beside
    it would add a second spelling that nothing checks and nothing uses, and on
    this installation it could not even be read off a pull request, because the
    account it describes has never opened one.

    So the entry covers the login form, and the census test refuses the id form
    by name rather than pretending to cover it: a workflow that switches to
    `…user.id` gets a red check telling it this file has to grow first.
    """

    login: str


@final
@dataclass(frozen=True)
class App:
    """One GitHub App this installation runs, under the facts that are public.

    An App is console-made and console-installed (framework/github.md §4), and
    what a workflow needs of it to mint an installation token is two things
    of opposite kinds: the private key, which authenticates as the App and is
    a register row escrowed and pushed as a repository secret (credentials.md
    §3), and the client id, which only names the App -- the JWT's issuer, an
    identifier GitHub shows on the App's page for as long as the App exists.
    The client id is on the same side of the fact/credential split as the
    tenancy OCID and the Cloudflare account id (`conventions.providers`): it
    opens nothing, so it is recorded here in the clear and declared into the
    repository variable the workflow reads it from. The key never appears
    here.

    The repositories an App is installed on are not a field of the App: an
    installation is console state nothing here declares, and what it decides
    for a repository -- which App can push to it -- is read off the
    repository's own row (`Repository.apps`).
    """

    #: The App's slug, as its settings page and its own login (`<slug>[bot]`)
    #: spell it.
    slug: str
    #: What the App's page shows as *Client ID*, and what a workflow hands to
    #: the minting action beside the key.
    client_id: str


@final
@dataclass(frozen=True)
class Variable:
    """One repository variable a workflow reads.

    A variable is the public half of a delivery whose other half is a secret:
    the value is a fact of this installation, so it is spelled here from the
    census row it belongs to rather than typed into a console. A workflow that
    reads a variable nothing declares fails the way an undeclared label does
    -- the expression is empty, and the step that receives it fails on its
    own terms -- so the set is written down here and a test holds the
    workflows to it.
    """

    #: As the workflow spells it under `vars.`.
    name: str
    #: Public. A value that would have to be a secret is a register row and a
    #: repository secret (credentials.md §3), never an entry here.
    value: str


@final
@dataclass(frozen=True)
class Repository:
    """One repository as this installation defines it, rather than as GitHub stores it.

    What it is, the cells of the credential partition it carries, and the
    switches its workflows branch on. Its settings are not here: they define no
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
    labels: tuple[Label, ...] = ()
    #: The accounts a workflow tests the author of a pull request against. Held
    #: beside the labels because it fails the same way and is caught the same
    #: way — a workflow comparing against a login nothing here names is a
    #: comparison that is never true — and on the row rather than in a table of
    #: its own, because these are this repository's workflows and no other's.
    authors: tuple[Author, ...] = ()
    #: The Apps installed on this repository, which is what decides whose
    #: installation token can push to it (credentials.md §3). Console state
    #: recorded rather than declared (framework/github.md §4), and on the row
    #: because a variable below that hands an App's client id to a mint is a
    #: mint that fails unless the App is installed here -- a relation between
    #: two fields of one row, which a test holds.
    apps: tuple[App, ...] = ()
    #: The repository variables its workflows read. Declared as resources like
    #: the labels, and for the same reason: a workflow reading a variable that
    #: was never set is one that fails at the step that needed it, on the
    #: first run after a rebuild.
    variables: tuple[Variable, ...] = ()

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


#: The escape hatch that opts a pull request out of the unattended merge
#: altogether (ci.md §3): a bump that is *supposed* to move resources wants a
#: human on the diff, so `noop-automerge.yml` stands down on this label rather
#: than proving anything.
EXPECT_CHANGES = Label('expect-changes', 'Opts a PR out of the noop-automerge zero-diff path (ci.md §3)')

#: Renovate as the API reports it: the hosted app's own login is what arrives
#: as the author of a pull request it opened, and a self-hosted instance, a
#: different app slug or a personal-access-token user would arrive as something
#: else. What branching on it buys is the proof-skipping route of
#: `noop-automerge.yml` (ci.md §3) and nothing else. The literal is recorded
#: here rather than left in the workflow alone because a workflow cannot notice
#: that it guessed wrong: the comparison is simply never true, and the route it
#: guards is never taken.
RENOVATE = Author('renovate[bot]')

#: The App whose installation token pushes where `GITHUB_TOKEN` must not: onto
#: a renovate branch from `sdk-regenerate.yml`, so that the pushed head's runs
#: start on their own (ci.md §3), and a `repository_dispatch` into the ops
#: repository from the alert producer, `alert.yml` (`conventions.alert`). Its
#: key is the `github-dispatch-key` register row; its client id is what the
#: variable below carries.
DISPATCH_APP = App(slug='kluster-dispatch', client_id='Iv23liR3WBm3bxvOVqch')

#: How a workflow minting the dispatch App's token learns which App its key
#: belongs to: the client id is handed to `actions/create-github-app-token`
#: beside the key, and this is the variable `sdk-regenerate.yml` and
#: `alert.yml` read it from.
DISPATCH_APP_CLIENT_ID = Variable('DISPATCH_APP_CLIENT_ID', DISPATCH_APP.client_id)

#: The label the ops repository's dispatch handler puts on every `actionable`
#: alert issue it opens, and the one it lists open issues by to find the issue
#: an alert's `key` already has (operations.md §4). Declared rather than made by
#: hand for the reason every label is, and because a hand-made label of the
#: same name collides with the declared one rather than being adopted by it.
ALERT = Label('alert', 'An alert issue: the dispatch handler applies it and lists open alerts by it (operations.md §4)')

#: The ops repository's only Environment, which carries the unattended drills'
#: credentials. Ungated because its scope is the gate (credentials.md §4).
#: Any branch, for want of an alternative: a private repository has no
#: protected branches to name on this plan (framework/github.md §2). Named
#: after the consumer whose credentials it holds (`identity.DRILL`): the
#: Environment, the IAM principal and the compartment are one name.
DRILL = Environment(identity.DRILL, BranchPolicy.ANY_BRANCH)

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
    labels=(EXPECT_CHANGES,),
    authors=(RENOVATE,),
    apps=(DISPATCH_APP,),
    variables=(DISPATCH_APP_CLIENT_ID,),
)

#: The notification and drill repository. Private on purpose — it holds the
#: alert issues and every scheduled workflow — which is what puts branch
#: protection and secret scanning out of its reach.
OPS = Repository(
    name='kluster-ops',
    public=False,
    environments=(DRILL,),
    labels=(ALERT,),
    # Installed for the alert producer, whose token dispatches into this
    # repository (credentials.md §3). The producer runs in `kluster` and reads
    # the client id from that repository's variable, so this row declares
    # none: a variable belongs to the repository whose workflow reads it.
    apps=(DISPATCH_APP,),
)

#: Every repository the forge declares, which is every repository of this
#: installation: a name absent here is one nothing in this tree manages.
REPOSITORIES = (DEPLOYMENT, OPS)
