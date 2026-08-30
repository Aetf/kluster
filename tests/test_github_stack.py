"""The `github` program: the forge declared as an estate of its own.

Every case here is about a setting that a later diff cannot show, because each
is a rule about what *cannot* happen: a check that is required, a push that is
refused, a repository a destroy may not delete.
"""

from typing import Any

import pulumi
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

OPERATOR_ID = '4242'

REPOSITORY = 'github:index/repository:Repository'
BRANCH_PROTECTION = 'github:index/branchProtection:BranchProtection'
ENVIRONMENT = 'github:index/repositoryEnvironment:RepositoryEnvironment'
VULNERABILITY_ALERTS = 'github:index/repositoryVulnerabilityAlerts:RepositoryVulnerabilityAlerts'


class Forge(Recorder):
    """GitHub as far as the program reads it back: node ids, and who the operator is."""

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == REPOSITORY:
            return {'nodeId': f'node_{args.name}', 'fullName': f'Aetf/{args.name}'}
        return {}

    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        if args.token == 'github:index/getUser:getUser':
            return {'id': OPERATOR_ID, 'login': 'Aetf'}
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


def test_the_previewed_layers_are_reachable_from_a_pull_request_branch(stack: Forge) -> None:
    """`preview.yml` runs these Environments on the PR's own branch.

    A protected-branches-only policy on them would fail every preview, which is
    the check the merge chain rests on.
    """
    environments = stack.by_name(ENVIRONMENT)

    for layer in ('dns', 'k8s-base', 'apps'):
        assert 'deploymentBranchPolicy' not in environments[layer], layer


def test_the_physical_environments_are_main_only(stack: Forge) -> None:
    # Their credentials can root the gateway, so a pull request's code never
    # runs with them (ci.md §3).
    environments = stack.by_name(ENVIRONMENT)

    for name in ('physical-plan', 'physical'):
        assert environments[name]['deploymentBranchPolicy'] == {
            'protectedBranches': True,
            'customBranchPolicies': False,
        }


def test_the_apply_environment_is_the_only_gated_one(stack: Forge) -> None:
    environments = stack.by_name(ENVIRONMENT)

    gated = {name for name, inputs in environments.items() if inputs.get('reviewers')}

    assert gated == {'physical'}
    assert environments['physical']['reviewers'] == [{'users': [int(OPERATOR_ID)]}]
    assert environments['physical']['canAdminsBypass'] is False


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

    assert set(repositories) == {'kluster', 'kluster-ops'}
    assert all(inputs['archiveOnDestroy'] is True for inputs in repositories.values())


def test_secret_scanning_is_only_claimed_where_the_plan_offers_it(stack: Forge) -> None:
    # Secret scanning is public-repository-or-paid; asking for it on the
    # private ops repository is an API error, not a stricter setting.
    repositories = stack.by_name(REPOSITORY)

    assert repositories['kluster']['securityAndAnalysis']['secretScanning'] == {'status': 'enabled'}
    assert 'securityAndAnalysis' not in repositories['kluster-ops']


def test_vulnerability_alerts_are_asked_for_where_the_provider_still_answers(stack: Forge) -> None:
    # The `Repository` field of the same name is deprecated in favour of this
    # resource; asking both ways is how a deprecation becomes a diff loop.
    alerts = stack.by_name(VULNERABILITY_ALERTS)

    assert set(alerts) == {'kluster', 'kluster-ops'}
    assert all(inputs['enabled'] is True for inputs in alerts.values())
    assert all('vulnerabilityAlerts' not in inputs for inputs in stack.by_name(REPOSITORY).values())
