"""The `github` program and the census it declares from.

Every case about the program is a setting that a later diff cannot show,
because each is a rule about what *cannot* happen: a check that is required, a
push that is refused, a repository a destroy may not delete. Two more are
about where a resource *is* rather than what it says: the provider that signs
it, and the URN it keeps.

The census (`conventions.forge`) is pinned here as well, in literals. The
program's cases read it, so a census that quietly changed would move what they
assert along with it -- and the `credentials` command reads the same table, so
a wrong entry is a secret pushed where no job will see it as readily as a
setting nobody meant.
"""

import re
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pulumi
import pytest
import pytest_asyncio
import root_credentials
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.forge import LABEL_COLOR, ManagedRepository
from kluster.stacks import github as program

WORKFLOWS = Path(__file__).parent.parent / '.github' / 'workflows'


def _workflows() -> list[Path]:
    """Every workflow file GitHub would run, which is both spellings of the suffix.

    GitHub reads `.yml` and `.yaml` out of this directory identically, so a
    census that globs one of them is one a file named the other way is invisible
    to -- and invisible is the state every case that uses it exists to prevent.
    """
    return sorted(WORKFLOWS.glob('*.yml')) + sorted(WORKFLOWS.glob('*.yaml'))


#: How a workflow condition names a label on the pull request it is running
#: for, which is the only way any of them reads a label.
LABEL_IN_A_CONDITION = re.compile(r"pull_request\.labels\.\*\.name,\s*'([^']+)'")

#: How a workflow compares who is behind the event against a login: one of the
#: contexts that carries one -- `github.actor`, `github.triggering_actor`, a
#: `user.login`, a `sender.login` -- either way round the comparison is
#: written. `!=` is matched as well as `==`, because a login misspelt in a
#: negative test fails *open*, which is the worse of the two directions to
#: leave unpinned. Two groups, one per way round, so a match carries the login
#: in whichever of them is not empty.
AUTHOR_IN_A_CONDITION = re.compile(
    r"\.(?:\w+_)?(?:actor|login)\s*[=!]=\s*'([^']+)'|'([^']+)'\s*[=!]=\s*[\w.]*(?:actor|login)\b"
)

#: The other way GitHub spells the same identity. `conventions.forge.Author`
#: carries a login and no id and says why, so a workflow reaching for the id
#: form is outside what the census covers -- and this is what makes that a red
#: check rather than a workflow that slipped past the scan above.
AUTHOR_BY_ID = re.compile(r'\.(?:user|sender)\.id\b|\.actor_id\b')

REPOSITORY = 'github:index/repository:Repository'
BRANCH_PROTECTION = 'github:index/branchProtection:BranchProtection'
ENVIRONMENT = 'github:index/repositoryEnvironment:RepositoryEnvironment'
VULNERABILITY_ALERTS = 'github:index/repositoryVulnerabilityAlerts:RepositoryVulnerabilityAlerts'
LABEL = 'github:index/issueLabel:IssueLabel'
MANAGED_REPOSITORY = 'kluster:components:forge:ManagedRepository'
PROVIDER = 'pulumi:providers:github'

#: Not the operator's: the test process holds no credential at all
#: (`root_credentials`), so this suite asks for a fake one by name.
TOKEN = root_credentials.fake(program.TOKEN_VARIABLE)

#: How a secret arrives on the wire: Pulumi's special-signature key, carrying
#: the signature that means "secret", beside the value itself.
SECRET = {'4dabf18193072939515e22adb298388d': '1b47061264138c4ac30d75fd1eb44270'}


class EveryRegistration(dict[str, Any]):
    """Every registration request, and not only the last under each logical name.

    The shared recorder keys them by logical name alone, and a repository is
    declared three times under its own name -- the component, the repository,
    and its vulnerability alerts -- so the last of the three would be the only
    one left. The requests are where a resource's *options* are, which is
    where an alias is, so the cases below need all of them.
    """

    def __init__(self) -> None:
        super().__init__()
        #: In registration order.
        self.every: list[Any] = []

    def __setitem__(self, name: str, request: Any) -> None:
        self.every.append(request)
        super().__setitem__(name, request)


class Forge(Recorder):
    """GitHub as far as the program reads it back, which is node ids and nothing else.

    The run makes no invoke, so this monitor answers none: what the program
    knows about the account itself, it knows from the census.
    """

    def __init__(self) -> None:
        super().__init__()
        #: The dict the shared recorder writes every registration into, under
        #: a name of its own so that the list beside it is reachable.
        self.requests = EveryRegistration()
        self.registrations = self.requests

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == REPOSITORY:
            return {'nodeId': f'node_{args.name}', 'fullName': f'{conventions.forge.ACCOUNT.login}/{args.name}'}
        return {}

    def request(self, typ: str, name: str) -> Any:
        """The registration request of one resource, named by type as well as by name."""
        found = [request for request in self.requests.every if (request.type, request.name) == (typ, name)]
        if len(found) != 1:
            raise AssertionError(f'{typ} {name!r} was registered {len(found)} times, not once')
        return found[0]


@pytest_asyncio.fixture(scope='module', autouse=True)
async def stack() -> AsyncGenerator[Forge]:
    """The whole program, declared once: every case below reads the same run."""
    with root_credentials.fake_credentials(program.TOKEN_VARIABLE):
        monitor = await run_with(Forge(), stack='github')
        async with declaring():
            await program.main()
        yield monitor


def test_the_census_names_the_two_repositories_and_what_the_plan_gives_each() -> None:
    """Visibility is the flag the plan's public-only features are derived from.

    A repository recorded under the wrong one asks for a feature it cannot
    have, or declines one it could.
    """
    assert {repository.name: repository.public for repository in conventions.forge.REPOSITORIES} == {
        'kluster': True,
        'kluster-ops': False,
    }
    assert conventions.forge.DEPLOYMENT.plan_offers_public_features is True
    assert conventions.forge.OPS.plan_offers_public_features is False
    assert conventions.forge.ACCOUNT == conventions.forge.Account(login='Aetf', user_id=1519759)
    assert conventions.forge.DEPLOYMENT.full_name == 'Aetf/kluster'


def test_the_census_carries_the_environments_the_merge_chain_runs() -> None:
    """The credential partition, written out: which Environments exist and what each admits.

    Order is part of it -- the credentials command reads this tuple as the
    order the chain runs in -- and so is the branch policy, which is what keeps
    a credential that can root the gateway off a pull request's own branch.
    """
    protected, any_branch = conventions.forge.BranchPolicy.PROTECTED_ONLY, conventions.forge.BranchPolicy.ANY_BRANCH

    assert [
        (environment.name, environment.branches, environment.gated)
        for environment in conventions.forge.DEPLOYMENT.environments
    ] == [
        ('physical-plan', protected, False),
        ('physical', protected, True),
        ('dns', any_branch, False),
        ('k8s-base', any_branch, False),
        ('apps', any_branch, False),
    ]
    # Ungated, because the drill's scope is the gate (credentials.md §4) and
    # this plan offers a private repository none anyway.
    assert conventions.forge.OPS.environments == (conventions.forge.DRILL,)
    assert conventions.forge.DRILL == conventions.forge.Environment('drill', any_branch, gated=False)


def test_every_label_a_workflow_branches_on_is_one_the_census_carries() -> None:
    """A label a workflow reads and nothing declares fails in the quietest way there is.

    The condition is simply never true, so the behaviour it guards is
    unavailable at the moment somebody needs it and nothing anywhere reports
    that. Reading the workflows is what keeps the census from being shorter
    than what they depend on.
    """
    declared = {label.name for repository in conventions.forge.REPOSITORIES for label in repository.labels}
    read = {label for workflow in _workflows() for label in LABEL_IN_A_CONDITION.findall(workflow.read_text())}

    # `expect-changes` stands noop-automerge down altogether (ci.md §3). A
    # census that lost it would leave a live condition pointing at a label no
    # pull request can carry, and that one fails open: the escape hatch is what
    # stops a deliberate change from merging on a proof it was never going to
    # pass.
    assert 'expect-changes' in read
    assert read <= declared, f'read by a workflow and declared nowhere: {sorted(read - declared)}'


def test_every_login_a_workflow_compares_against_is_one_the_census_names() -> None:
    """A login a workflow compares against and nothing names is a route that never fires.

    Which is indistinguishable from a route nobody has needed yet, so nothing
    reports it. `renovate[bot]` is the hosted app's own login; a self-hosted
    instance, a different app slug or a personal-access-token user arrives
    under another one, and the difference is invisible until a pull request
    that should have taken the route quietly does not. Naming the login is what
    makes a wrong literal a red check instead.

    **What this reaches is a login written as a literal beside a comparison**,
    which is how every workflow here spells it and what the case below holds
    still. It is not a proof that no workflow can consult an identity any other
    way: a `startsWith`, a login inside a `fromJSON` list, and a literal parked
    in `env:` and compared in the shell all read as ordinary text to it. The id
    spelling is the one exception, refused by name in the case after this,
    because that is the substitution a workflow is most likely to make on
    purpose.
    """
    named = {author.login for repository in conventions.forge.REPOSITORIES for author in repository.authors}
    read = {
        login
        for workflow in _workflows()
        for match in AUTHOR_IN_A_CONDITION.findall(workflow.read_text())
        for login in match
        if login
    }

    # noop-automerge's unproven route is renovate's and nobody else's (ci.md
    # §3), so a workflow that stopped naming the login, or a census that
    # stopped carrying it, is what this holds still.
    assert 'renovate[bot]' in read
    assert read <= named, f'compared against by a workflow and named nowhere: {sorted(read - named)}'


def test_no_workflow_identifies_an_account_by_id() -> None:
    """`conventions.forge.Author` carries a login and no id, and says why.

    A workflow that switches to the id form is reaching for a spelling the
    census does not carry -- and one the case above cannot see, since it reads
    logins written as literals. Refusing it by name is what keeps that a red
    check with a reason on it, instead of a census that silently stopped
    covering the condition it exists for.
    """
    reached = {workflow.name for workflow in _workflows() if AUTHOR_BY_ID.search(workflow.read_text())}

    assert not reached, f'identifies an account by id, which conventions.forge.Author does not carry: {sorted(reached)}'


def test_main_requires_the_two_checks_that_always_run(stack: Forge) -> None:
    """A required check that only sometimes runs blocks a pull request forever.

    `checks` and `changes` both run on every pull request to main regardless of
    paths; the `preview` matrix does not, and carries the stack name in its
    check name besides.
    """
    protection = stack.by_name(BRANCH_PROTECTION)['main']

    assert protection['requiredStatusChecks'] == [{'strict': True, 'contexts': ['checks', 'changes']}]


def test_the_owner_cannot_walk_around_the_gate(stack: Forge) -> None:
    # The installation has one admin, so an unenforced protection is no
    # protection: it would be bypassed by exactly the person it applies to.
    protection = stack.by_name(BRANCH_PROTECTION)['main']

    assert protection['enforceAdmins'] is True
    assert protection['allowsForcePushes'] is False
    assert protection['allowsDeletions'] is False


def test_every_environment_the_census_names_is_declared(stack: Forge) -> None:
    """The census is what exists: the program adds none of its own and drops none.

    An Environment the credentials command pushes a secret into and this stack
    never creates is a push that fails; one this stack creates and the census
    does not name is an Environment nothing fills.
    """
    census = {
        environment.name for repository in conventions.forge.REPOSITORIES for environment in repository.environments
    }

    assert set(stack.by_name(ENVIRONMENT)) == census


def test_a_branch_policy_is_declared_exactly_where_the_census_asks_for_one(stack: Forge) -> None:
    """A protected-branches policy is what keeps a credential off a pull request's code.

    `physical-plan` and `physical` carry one because theirs can root the
    gateway (ci.md §3). The previewed layers carry none, deliberately:
    `preview.yml` runs those Environments on a pull request's own branch, so a
    policy would fail every preview -- the check the merge chain rests on.
    """
    environments = stack.by_name(ENVIRONMENT)

    for repository in conventions.forge.REPOSITORIES:
        for entry in repository.environments:
            main_only = entry.branches is conventions.forge.BranchPolicy.PROTECTED_ONLY
            declared = environments[entry.name].get('deploymentBranchPolicy')

            assert declared == ({'protectedBranches': True, 'customBranchPolicies': False} if main_only else None), (
                entry.name
            )


def test_a_reviewer_stands_in_front_of_exactly_the_gated_environments(stack: Forge) -> None:
    environments = stack.by_name(ENVIRONMENT)
    census = {
        entry.name for repository in conventions.forge.REPOSITORIES for entry in repository.environments if entry.gated
    }

    gated = {name for name, inputs in environments.items() if inputs.get('reviewers')}

    assert gated == census
    for name in gated:
        assert environments[name]['reviewers'] == [{'users': [conventions.forge.ACCOUNT.user_id]}]
        # Off for the same reason `enforce_admins` is on: a door with a key
        # under the mat is not a door.
        assert environments[name]['canAdminsBypass'] is False


def test_the_run_asks_the_account_nothing(stack: Forge) -> None:
    # Everything the program needs to name the account is a census constant, so
    # nothing has to be resolved before it can declare. An invoke is also the
    # one call that needs a parent named for it before it can inherit a
    # provider at all.
    assert stack.call_providers == {}


def test_merges_are_rebases_only(stack: Forge) -> None:
    """A squash rewrites authorship to the merging identity.

    Which for noop-automerge is `noreply@github.com`, and a merge commit would
    contradict the linear history the branch protection asks for.
    """
    for repository in stack.by_name(REPOSITORY).values():
        assert repository['allowRebaseMerge'] is True
        assert repository['allowSquashMerge'] is False
        assert repository['allowMergeCommit'] is False


def test_destroying_the_stack_cannot_delete_the_repositories(stack: Forge) -> None:
    repositories = stack.by_name(REPOSITORY)

    assert set(repositories) == {repository.name for repository in conventions.forge.REPOSITORIES}
    assert all(inputs['archiveOnDestroy'] is True for inputs in repositories.values())


def test_each_repository_is_declared_with_the_visibility_the_census_records(stack: Forge) -> None:
    repositories = stack.by_name(REPOSITORY)

    for repository in conventions.forge.REPOSITORIES:
        assert repositories[repository.name]['visibility'] == ('public' if repository.public else 'private')


def test_secret_scanning_is_only_claimed_where_the_plan_offers_it(stack: Forge) -> None:
    # Which repositories the plan offers it to, and why, is the census's
    # derived answer (`plan_offers_public_features`); what this pins is that
    # the program asks that rather than deciding at each repository's own line.
    repositories = stack.by_name(REPOSITORY)

    for repository in conventions.forge.REPOSITORIES:
        inputs = repositories[repository.name]
        if repository.plan_offers_public_features:
            assert inputs['securityAndAnalysis']['secretScanning'] == {'status': 'enabled'}
        else:
            assert 'securityAndAnalysis' not in inputs


def test_vulnerability_alerts_are_asked_for_where_the_provider_still_answers(stack: Forge) -> None:
    # The `Repository` field of the same name is deprecated in favour of this
    # resource; asking both ways is how a deprecation becomes a diff loop.
    alerts = stack.by_name(VULNERABILITY_ALERTS)

    assert set(alerts) == {'kluster', 'kluster-ops'}
    assert all(inputs['enabled'] is True for inputs in alerts.values())
    assert all('vulnerabilityAlerts' not in inputs for inputs in stack.by_name(REPOSITORY).values())


def test_the_provider_is_built_here_and_signs_every_resource(stack: Forge) -> None:
    """Nothing rides the ambient provider, which is what the stack config now forbids.

    A default provider configures itself from the environment and, with no
    token there, `pulumi_github` runs *anonymously*: the failure is a write
    refused partway through rather than a run that never starts. One provider
    built in the program, inherited through each component, is what makes the
    absence of the credential a stop instead.
    """
    provider = stack.by_name(PROVIDER)

    assert set(provider) == {f'{conventions.CLUSTER_NAME}-github'}
    # The account is named from the census, not from an invoke that resolves it.
    assert provider[f'{conventions.CLUSTER_NAME}-github']['owner'] == conventions.forge.ACCOUNT.login

    signed = {declaration.provider for declaration in stack.declared if declaration.typ.startswith('github:')}

    assert '' not in signed, 'a resource was registered against the ambient provider'
    # The two repositories are two trees against one account, so one provider
    # signs both -- reached through each component's options, never re-plumbed
    # onto a resource.
    assert len(signed) == 1
    assert f'::{conventions.CLUSTER_NAME}-github::' in signed.pop()


def test_the_token_reaches_the_provider_from_the_environment_and_marked_secret(stack: Forge) -> None:
    """The credential the provider opens with is the variable's value, and state never sees it.

    Two claims in one line, because they fail the same way. The program reads
    the variable itself rather than leaving the SDK to find it, and the
    marking that keeps an account root out of state in the clear is the
    generated provider's own -- so this is what would notice a release that
    stopped applying it, before a state file did.
    """
    assert stack.by_name(PROVIDER)[f'{conventions.CLUSTER_NAME}-github']['token'] == SECRET | {'value': TOKEN}


@pytest.mark.asyncio
async def test_a_run_without_the_token_refuses_by_name() -> None:
    """The absence of the credential is what keeps this stack from being applied by accident.

    So it has to be a refusal that names the variable, not a run that
    authenticates as nobody and discovers it on the first write.
    """
    # What this takes away is the fake `stack` asked for, not the operator's
    # credential -- the process holds none of those at all
    # (`root_credentials`), so unsetting it here restores the default rather
    # than departing from it. Unset rather than left absent because `stack` is
    # autouse and its block is open around this case too, and `raising` is
    # left at its default so that a suite that stopped asking for a fake would
    # fail here instead of passing for the wrong reason.
    with pytest.MonkeyPatch.context() as patched:
        patched.delenv(program.TOKEN_VARIABLE)
        monitor = await run_with(Forge(), stack='github')

        with pytest.raises(ValueError, match=program.TOKEN_VARIABLE):
            await program.main()

    assert monitor.declared == [], 'the refusal must come before anything is declared'


def test_each_repository_keeps_the_urn_it_was_declared_at(stack: Forge) -> None:
    """Introducing the component moved every URN down a level, and an alias is what makes that a rename.

    Without one the preview is "create the parented one, delete the
    unparented one", and the delete of a `protect`ed repository is refused.
    One alias per repository is enough for its whole subtree: everything the
    component declares is parented on the repository rather than on the
    component, so each of them inherits the repository's alias and lands back
    on the URN it already has.
    """
    for entry in conventions.forge.REPOSITORIES:
        aliases = list(stack.request(REPOSITORY, entry.name).aliases)

        assert len(aliases) == 1, entry.name
        # Everything else left at its default, which reads as "same name, same
        # type, this stack, this project" -- so the alias is exactly the URN
        # this repository had when the stack program declared it itself.
        assert aliases[0].spec.noParent is True
        assert (aliases[0].spec.name, aliases[0].spec.type, aliases[0].spec.stack, aliases[0].spec.project) == (
            '',
            '',
            '',
            '',
        )


def test_nothing_below_a_repository_moved(stack: Forge) -> None:
    """The subtree's URNs are preserved by parenting, not by a second alias each.

    Each resource under a repository names the repository as its parent, which
    is both what it is -- a property of that repository -- and what makes the
    one alias above cover it. A resource re-parented onto the component would
    silently need an alias of its own.
    """
    for entry in conventions.forge.REPOSITORIES:
        assert stack.request(REPOSITORY, entry.name).parent.endswith(f'{MANAGED_REPOSITORY}::{entry.name}')

        # The repository's own URN, as its children carry it: the component's
        # type, then the repository's, then the repository's name.
        repository = stack.request(VULNERABILITY_ALERTS, entry.name).parent
        assert repository.endswith(f'{MANAGED_REPOSITORY}${REPOSITORY}::{entry.name}')

        for typ, name in _below(entry):
            assert stack.request(typ, name).parent == repository, name
            assert list(stack.request(typ, name).aliases) == [], name


def _below(entry: conventions.forge.Repository) -> list[tuple[str, str]]:
    """Every resource `ManagedRepository` hangs off one repository, by type and name."""
    below = [(VULNERABILITY_ALERTS, entry.name)]
    below += [(LABEL, f'{entry.name}-{label.name}') for label in entry.labels]
    below += [(ENVIRONMENT, environment.name) for environment in entry.environments]
    if entry is conventions.forge.DEPLOYMENT:
        below.append((BRANCH_PROTECTION, 'main'))
    return below


def test_each_label_a_workflow_branches_on_is_a_declared_resource(stack: Forge) -> None:
    """A label made by hand is one the next rebuild does not have.

    The workflow that reads it then fails in the quietest way there is -- the
    condition is never true and nothing reports it -- so every label is
    declared from the census like everything else here, name and description
    both: the description is what the operator reaching for one reads.
    """
    labels = stack.by_name(LABEL)

    # Written out, because everything below this line is derived from the
    # census on both sides: a census that lost its labels would leave the
    # comparison `set() == set()` and the loop body unentered, and the whole
    # case would pass having asserted nothing (ops#184).
    assert set(labels) == {'kluster-expect-changes'}
    assert set(labels) == {
        f'{repository.name}-{label.name}'
        for repository in conventions.forge.REPOSITORIES
        for label in repository.labels
    }
    for repository in conventions.forge.REPOSITORIES:
        for label in repository.labels:
            declared = labels[f'{repository.name}-{label.name}']
            assert declared['name'] == label.name
            assert declared['description'] == label.description
            assert declared['color'] == LABEL_COLOR
            assert declared['repository'] == repository.name


def test_only_the_repository_that_merges_unattended_offers_auto_merge(stack: Forge) -> None:
    """Auto-merge and branch updating are what noop-automerge needs, and only it needs them.

    One queues a merge behind the checks; the other lets a pull request
    satisfy "must be up to date" without a human. The ops repository merges
    nothing unattended, so it asks for neither.
    """
    repositories = stack.by_name(REPOSITORY)

    assert repositories[conventions.forge.DEPLOYMENT.name]['allowAutoMerge'] is True
    assert repositories[conventions.forge.DEPLOYMENT.name]['allowUpdateBranch'] is True
    assert 'allowAutoMerge' not in repositories[conventions.forge.OPS.name]
    assert 'allowUpdateBranch' not in repositories[conventions.forge.OPS.name]


@pytest.mark.asyncio
async def test_required_checks_on_a_repository_the_plan_cannot_guard_are_refused() -> None:
    """Branch protection is public-repository-or-paid on this account.

    Naming required checks where none can be enforced would leave a repository
    looking guarded and not be, which is the same quiet failure the declared
    label exists to prevent.
    """
    _ = await run_with(Forge(), stack='github')

    with pytest.raises(ValueError, match='no branch protection'):
        _ = ManagedRepository(
            conventions.forge.OPS.name,
            entry=conventions.forge.OPS,
            description='a private repository',
            required_checks=('checks',),
        )
