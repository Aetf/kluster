"""The `github` program: the forge declared as an estate of its own.

Every case here is about a setting that a later diff cannot show, because each
is a rule about what *cannot* happen: a check that is required, a push that is
refused, a repository a destroy may not delete.
"""

from typing import Any

import pulumi
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions

REPOSITORY = 'github:index/repository:Repository'
BRANCH_PROTECTION = 'github:index/branchProtection:BranchProtection'
ENVIRONMENT = 'github:index/repositoryEnvironment:RepositoryEnvironment'
VULNERABILITY_ALERTS = 'github:index/repositoryVulnerabilityAlerts:RepositoryVulnerabilityAlerts'


class Forge(Recorder):
    """GitHub as far as the program reads it back, which is node ids and nothing else.

    The operator's own id is not among them: it is a census constant
    (`conventions.forge`), so the run asks GitHub who the operator is at no
    point and this monitor answers no invoke.
    """

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == REPOSITORY:
            return {'nodeId': f'node_{args.name}', 'fullName': f'{conventions.forge.OWNER}/{args.name}'}
        return {}


@pytest_asyncio.fixture(scope='module', autouse=True)
async def stack() -> Forge:
    """The whole program, declared once: every case below reads the same run."""
    from kluster.stacks import github

    monitor = await run_with(Forge(), stack='github')
    async with declaring():
        await github.main()
    return monitor


def test_main_requires_the_two_checks_that_always_run(stack: Forge) -> None:
    """A required check that only sometimes runs blocks a pull request forever.

    `checks` and `changes` both run on every pull request to main regardless of
    paths; the `preview` matrix does not, and carries the stack name in its
    check name besides.
    """
    protection = stack.by_name(BRANCH_PROTECTION)['main']

    assert protection['requiredStatusChecks'] == [{'strict': True, 'contexts': ['checks', 'changes']}]


def test_the_owner_cannot_walk_around_the_gate(stack: Forge) -> None:
    # The estate has one admin, so an unenforced protection is no protection:
    # it would be bypassed by exactly the person it applies to.
    protection = stack.by_name(BRANCH_PROTECTION)['main']

    assert protection['enforceAdmins'] is True
    assert protection['allowsForcePushes'] is False
    assert protection['allowsDeletions'] is False


def test_every_environment_the_census_names_is_declared(stack: Forge) -> None:
    """The census is what exists; the program adds none of its own and drops none.

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

    `physical-plan` and `physical` carry one because their credentials can root
    the gateway (ci.md §3). The previewed layers carry none, deliberately:
    `preview.yml` runs those Environments on a pull request's own branch, so a
    policy would fail every preview — the check the merge chain rests on.
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
        assert environments[name]['reviewers'] == [{'users': [conventions.forge.OPERATOR_ID]}]
        # Admin bypass off for the same reason `enforce_admins` is on: a door
        # with a key under the mat is not a door.
        assert environments[name]['canAdminsBypass'] is False


def test_the_run_asks_github_nothing(stack: Forge) -> None:
    # The operator's user id is the only thing this program ever read back from
    # the account, and it is a census constant now. An invoke would also be the
    # one call that needs a parent named for it to inherit a provider at all.
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


def test_secret_scanning_is_only_claimed_where_the_plan_offers_it(stack: Forge) -> None:
    # Public-repository-or-paid on this account, so asking for it on the
    # private ops repository is an API error, not a stricter setting. Which is
    # why the program asks the census rather than each repository's own line.
    repositories = stack.by_name(REPOSITORY)

    for repository in conventions.forge.REPOSITORIES:
        inputs = repositories[repository.name]
        if repository.plan_offers_public_features:
            assert inputs['securityAndAnalysis']['secretScanning'] == {'status': 'enabled'}
        else:
            assert 'securityAndAnalysis' not in inputs


def test_visibility_is_what_the_census_says(stack: Forge) -> None:
    # The flag the plan's features are derived from, so a repository whose
    # declared visibility disagreed with it would ask for a feature it cannot
    # have, or decline one it could.
    repositories = stack.by_name(REPOSITORY)

    assert {name: inputs['visibility'] for name, inputs in repositories.items()} == {
        'kluster': 'public',
        'kluster-ops': 'private',
    }


def test_vulnerability_alerts_are_asked_for_where_the_provider_still_answers(stack: Forge) -> None:
    # The `Repository` field of the same name is deprecated in favour of this
    # resource; asking both ways is how a deprecation becomes a diff loop.
    alerts = stack.by_name(VULNERABILITY_ALERTS)

    assert set(alerts) == {'kluster', 'kluster-ops'}
    assert all(inputs['enabled'] is True for inputs in alerts.values())
    assert all('vulnerabilityAlerts' not in inputs for inputs in stack.by_name(REPOSITORY).values())
