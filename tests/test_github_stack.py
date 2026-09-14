"""The `github` program and the census it declares from.

Every case about the program is a setting that a later diff cannot show,
because each is a rule about what *cannot* happen: a check that is required, a
push that is refused, a repository a destroy may not delete. Two more are
about where a resource *is* rather than what it says: the provider that signs
it, and the URN it keeps.

The census these cases read (`conventions.forge`) is held still in
`test_conventions`, where every census's invariants and seam tests are
(style/pulumi.md). It is out of this module deliberately as well as by the
rule -- the fixture below is `autouse`, so a check kept here would be
unreachable at exactly the moment it is wanted, which is when this program
fails to run.
"""

import inspect
from collections.abc import AsyncGenerator
from typing import Any

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.scripts.credentials import devices
from kluster.components.forge import LABEL_COLOR, ManagedRepository
from kluster.stacks import github as program


REPOSITORY = 'github:index/repository:Repository'
BRANCH_PROTECTION = 'github:index/branchProtection:BranchProtection'
ENVIRONMENT = 'github:index/repositoryEnvironment:RepositoryEnvironment'
VULNERABILITY_ALERTS = 'github:index/repositoryVulnerabilityAlerts:RepositoryVulnerabilityAlerts'
LABEL = 'github:index/issueLabel:IssueLabel'
MANAGED_REPOSITORY = 'kluster:components:forge:ManagedRepository'
PROVIDER = 'pulumi:providers:github'

#: Not the operator's: the token this stack opens with is a secret in its own
#: committed configuration, and a suite sets a stand-in that says as much if it
#: ever reaches a diff.
TOKEN = 'a-fake-github-admin-token-that-opens-nothing'

#: How a secret arrives on the wire: Pulumi's special-signature key, carrying
#: the signature that means "secret", beside the value itself.
SECRET = {'4dabf18193072939515e22adb298388d': '1b47061264138c4ac30d75fd1eb44270'}


class Forge(Recorder):
    """GitHub as far as the program reads it back, which is node ids and nothing else.

    The run makes no invoke, so this monitor answers none: what the program
    knows about the account itself, it knows from the census.
    """

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == REPOSITORY:
            return {'nodeId': f'node_{args.name}', 'fullName': f'{conventions.forge.ACCOUNT.login}/{args.name}'}
        return {}


@pytest_asyncio.fixture(scope='module', autouse=True)
async def stack() -> AsyncGenerator[Forge]:
    """The whole program, declared once: every case below reads the same run."""
    pulumi.runtime.set_all_config({f'kluster:{program.ADMIN_TOKEN}': TOKEN})
    monitor = await run_with(Forge(), stack='github')
    async with declaring():
        await program.main()
    yield monitor


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
    repositories = stack.by_name(REPOSITORY)

    # A loop is only a claim about what it visits, so the run is held to the
    # census before it is walked: a run that declared no repository would
    # satisfy the loop and nothing else here.
    assert set(repositories) == {repository.name for repository in conventions.forge.REPOSITORIES}
    for repository in repositories.values():
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


def test_the_token_reaches_the_provider_from_this_stacks_config_and_marked_secret(stack: Forge) -> None:
    """The credential the provider opens with is the config key's value, and state never sees it.

    Two claims in one line, because they fail the same way. The program reads
    the key itself rather than leaving the SDK to find one, and the marking
    that keeps the token out of state in the clear is the generated provider's
    own -- so this is what would notice a release that stopped applying it,
    before a state file did.
    """
    assert stack.by_name(PROVIDER)[f'{conventions.CLUSTER_NAME}-github']['token'] == SECRET | {'value': TOKEN}


def test_the_config_key_is_this_projects_own_rather_than_the_providers() -> None:
    """Bare, so `pulumi.Config()` resolves it against this project's namespace.

    A `github:` key would be the provider package's ambient configuration,
    which this repository has removed everywhere else and which reads as
    something no reviewer can distinguish from it (`stacks/dns.py`).
    """
    assert ':' not in program.ADMIN_TOKEN


@pytest.mark.asyncio
async def test_a_run_without_the_token_refuses_by_name_and_names_what_fills_it() -> None:
    """An unconfigured stack must stop before it declares anything.

    `pulumi_github` would otherwise authenticate as nobody and discover the
    absence on its first write. The refusal also names the command that fills
    the key, because the checkout most likely to meet it is one whose stack
    file predates the key -- an operator part-way across the crossing
    (credentials.md §3), for whom `pulumi config set` is the wrong answer.
    """
    # `stack` is autouse and module-scoped, so the configuration this takes
    # away has to go back whatever happens here: a failed assertion would
    # otherwise run every later case in the module against an empty config.
    try:
        pulumi.runtime.set_all_config({})
        monitor = await run_with(Forge(), stack='github')

        with pytest.raises(ValueError, match=program.ADMIN_TOKEN) as refusal:
            await program.main()

        # Read off the register rather than typed here: renaming the row moves
        # both copies, where a hand-written literal would go on matching a
        # message that had stopped naming a command that exists
        # (`docs/style/testing.md`).
        record = f'credentials derived {devices.DEVICES[devices.GITHUB_ADMIN].member} record'
        assert record in str(refusal.value)
        assert monitor.declared == [], 'the refusal must come before anything is declared'
    finally:
        pulumi.runtime.set_all_config({f'kluster:{program.ADMIN_TOKEN}': TOKEN})


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
        aliases = list(stack.options_of(entry.name, REPOSITORY).aliases)

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
        assert stack.options_of(entry.name, REPOSITORY).parent.endswith(f'{MANAGED_REPOSITORY}::{entry.name}')

        # The repository's own URN, as its children carry it: the component's
        # type, then the repository's, then the repository's name.
        repository = stack.options_of(entry.name, VULNERABILITY_ALERTS).parent
        assert repository.endswith(f'{MANAGED_REPOSITORY}${REPOSITORY}::{entry.name}')

        for typ, name in _below(entry):
            assert stack.options_of(name, typ).parent == repository, name
            assert list(stack.options_of(name, typ).aliases) == [], name


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


def test_the_required_checks_are_a_roll_every_repository_states() -> None:
    """A census parameter has no default (style/pulumi.md).

    The roll decides whether `main` is protected at all, so a default of
    "none" is a default of "unguarded": a call site that lost the argument
    would delete the deployment repository's branch protection with the
    component raising nothing — only this suite's pins on `main` would notice.
    The repository that asks for no protection passes the empty roll.
    """
    parameter = inspect.signature(ManagedRepository.__init__).parameters['required_checks']
    assert parameter.default is inspect.Parameter.empty


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
