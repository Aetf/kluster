from typing import Any, cast

import pulumi
import pytest_asyncio
from pulumi.runtime.stack import wait_for_rpcs

OPERATOR_ID = '4242'

REPOSITORY = 'github:index/repository:Repository'
BRANCH_PROTECTION = 'github:index/branchProtection:BranchProtection'
ENVIRONMENT = 'github:index/repositoryEnvironment:RepositoryEnvironment'

#: Every resource the program declared: (type, logical name, inputs).
declared: list[tuple[str, str, dict[str, Any]]] = []


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        declared.append((args.typ, args.name, outputs))
        if args.typ == REPOSITORY:
            outputs['nodeId'] = f'node_{args.name}'
            outputs['fullName'] = f'Aetf/{args.name}'
        return args.name + '_id', outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        if args.token == 'github:index/getUser:getUser':
            return {'id': OPERATOR_ID, 'login': 'Aetf'}, []
        return {}, []


@pytest_asyncio.fixture(scope='module', autouse=True)
async def stack() -> None:
    """Declared once, on one event loop.

    Once because the assertions read one program's output, not eight; the
    session-wide event loop that makes that safe is set in pyproject.toml.
    """
    pulumi.runtime.set_mocks(Mocks(), project='kluster', stack='github', preview=False)
    from kluster.stacks import github

    await github.main()
    # Declaring a resource only schedules its registration; without draining
    # those the mocks have seen nothing and every assertion below would pass
    # vacuously. Registrations only: the outstanding-task queue this would
    # otherwise drain is process-global, and holds the deliberately failing
    # outputs another module built.
    await wait_for_rpcs(await_all_outstanding_tasks=False)


def _one(typ: str, name: str) -> dict[str, Any]:
    return next(inputs for kind, resource, inputs in declared if kind == typ and resource == name)


def _all(typ: str) -> dict[str, dict[str, Any]]:
    return {name: inputs for kind, name, inputs in declared if kind == typ}


def test_main_requires_the_two_checks_that_always_run() -> None:
    """A required check that only sometimes runs blocks a pull request forever.

    `checks` and `changes` both run on every pull request to main regardless of
    paths; the `preview` matrix does not, and carries the stack name in its
    check name besides.
    """
    protection = _one(BRANCH_PROTECTION, 'main')

    assert protection['requiredStatusChecks'] == [{'strict': True, 'contexts': ['checks', 'changes']}]


def test_the_owner_cannot_walk_around_the_gate() -> None:
    # The estate has one admin, so an unenforced protection is no protection:
    # it would be bypassed by exactly the person it applies to.
    protection = _one(BRANCH_PROTECTION, 'main')

    assert protection['enforceAdmins'] is True
    assert protection['allowsForcePushes'] is False
    assert protection['allowsDeletions'] is False


def test_the_previewed_layers_are_reachable_from_a_pull_request_branch() -> None:
    """`preview.yml` runs these Environments on the PR's own branch.

    A protected-branches-only policy on them would fail every preview, which is
    the check the merge chain rests on.
    """
    environments = _all(ENVIRONMENT)

    for layer in ('dns', 'k8s-base', 'apps'):
        assert 'deploymentBranchPolicy' not in environments[layer], layer


def test_the_physical_environments_are_main_only() -> None:
    # Their credentials can root the gateway, so a pull request's code never
    # runs with them (ci.md §3).
    environments = _all(ENVIRONMENT)

    for name in ('physical-plan', 'physical'):
        assert environments[name]['deploymentBranchPolicy'] == {
            'protectedBranches': True,
            'customBranchPolicies': False,
        }


def test_the_apply_environment_is_the_only_gated_one() -> None:
    environments = _all(ENVIRONMENT)
    gated = {name for name, inputs in environments.items() if inputs.get('reviewers')}

    assert gated == {'physical'}
    assert environments['physical']['reviewers'] == [{'users': [int(OPERATOR_ID)]}]
    assert environments['physical']['canAdminsBypass'] is False


def test_merges_are_rebases_only() -> None:
    """A squash rewrites authorship to the merging identity.

    Which for noop-automerge is `noreply@github.com`, and a merge commit would
    contradict the linear history the branch protection asks for.
    """
    for repository in ('kluster', 'kluster-ops'):
        inputs = _one(REPOSITORY, repository)
        assert inputs['allowRebaseMerge'] is True
        assert inputs['allowSquashMerge'] is False
        assert inputs['allowMergeCommit'] is False


def test_destroying_the_stack_cannot_delete_the_repositories() -> None:
    for repository in ('kluster', 'kluster-ops'):
        assert _one(REPOSITORY, repository)['archiveOnDestroy'] is True


def test_secret_scanning_is_only_claimed_where_the_plan_offers_it() -> None:
    # Secret scanning is public-repository-or-paid; asking for it on the
    # private ops repository is an API error, not a stricter setting.
    assert _one(REPOSITORY, 'kluster')['securityAndAnalysis']['secretScanning'] == {'status': 'enabled'}
    assert 'securityAndAnalysis' not in _one(REPOSITORY, 'kluster-ops')
