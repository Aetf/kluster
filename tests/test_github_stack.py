"""The `github` program and the census it declares from.

Every case about the program is a setting that a later diff cannot show,
because each is a rule about what *cannot* happen: a check that is required, a
push that is refused, a repository a destroy may not delete.

The census (`conventions.forge`) is pinned here as well, in literals. The
program's cases read it, so a census that quietly changed would move what they
assert along with it -- and the `credentials` command reads the same table, so
a wrong entry is a secret pushed where no job will see it as readily as a
setting nobody meant.
"""

import re
from pathlib import Path
from typing import Any

import pulumi
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions

WORKFLOWS = Path(__file__).parent.parent / '.github' / 'workflows'

#: How a workflow condition names a label on the pull request it is running
#: for, which is the only way any of them reads a label.
LABEL_IN_A_CONDITION = re.compile(r"pull_request\.labels\.\*\.name,\s*'([^']+)'")

REPOSITORY = 'github:index/repository:Repository'
BRANCH_PROTECTION = 'github:index/branchProtection:BranchProtection'
ENVIRONMENT = 'github:index/repositoryEnvironment:RepositoryEnvironment'
VULNERABILITY_ALERTS = 'github:index/repositoryVulnerabilityAlerts:RepositoryVulnerabilityAlerts'


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
async def stack() -> Forge:
    """The whole program, declared once: every case below reads the same run."""
    from kluster.stacks import github

    monitor = await run_with(Forge(), stack='github')
    async with declaring():
        await github.main()
    return monitor


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
    declared = {label for repository in conventions.forge.REPOSITORIES for label in repository.labels}
    read = {
        label
        for workflow in sorted(WORKFLOWS.glob('*.yml'))
        for label in LABEL_IN_A_CONDITION.findall(workflow.read_text())
    }

    # `expect-changes` opts a pull request out of the zero-diff proof
    # (ci.md §3); it is the only one any workflow reads today, and a census
    # that lost it would take the escape hatch with it.
    assert 'expect-changes' in read
    assert read <= declared, f'read by a workflow and declared nowhere: {sorted(read - declared)}'


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
