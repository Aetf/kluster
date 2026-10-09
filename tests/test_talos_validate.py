"""Talos accepts the machine configuration the Talos component renders.

`test_talos_config.py` holds the component's patches to the design; this
holds what they add up to against Talos itself. A patch can say exactly what
the design asks for and still be refused by the release that has to boot it
-- a `LinkConfig` route written as `destination: 0.0.0.0/0` is one, which
Talos rejects as an unspecified prefix -- and nothing short of Talos' own
validation sees that before the first `up`.

Each node shape is rendered the way the provider renders it: the base
configuration `talosctl gen config` generates for the pinned release's
contract, from one secrets bundle, with the component's patches applied in
the order `talos.patches` lists them. That is the machinery the provider's
`talos_machine_configuration` data source calls, with the provider's own
generation options (cluster discovery on, no documentation or examples in the
output). The result then goes through `talosctl validate --strict`, in the
mode the node's platform runs in.

`talosctl` is the one `mise.toml` pins, and it has to be the fleet's release
(`versions:talos` in `Pulumi.yaml`): the validation is the fleet release's only
if that release does it. It is stricter than a node on warnings, which a
node's apply returns without refusing the document, and it does not reach the
checks a node makes against its own running state on top of the document.
A missing binary fails rather than skips
-- a skip would leave `checks` green with nothing validated, and `checks`
installs every tool `mise.toml` pins.

The shapes are built here, from the inputs the physical stack passes, and
rendered through `talos.patches`, the component's public function;
`test_talos_config.py` keeps a table of its own. A table the two shared
would live in a named module under `tests/`, since no test module imports
another (framework/testing.md §2).
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import pinned_tools
import pytest
import yaml
from renovate_text import listed, package_rules, scalar

from kluster import conventions
from kluster.components import talos
from kluster.components.talos import image

ROOT = Path(__file__).parent.parent

#: The `[tools]` key `mise.toml` pins talosctl under, which is also the name
#: renovate's mise manager gives the dependency.
TOOL = 'talosctl'

#: The Pulumi config key holding the fleet's Talos release.
TALOS_KEY = 'versions:talos'

#: What the cluster endpoint and the balancer's SAN stand in for: the address
#: the cloud assigns the balancer, which no configuration here names. Any
#: address serves, since validation checks that the endpoint is a URL and the
#: SAN an address, not which ones.
BALANCER = '203.0.113.10'

#: How `talosctl validate` is told which platform checks to apply.
Mode = Literal['cloud', 'metal']

#: The mode each Talos platform a node boots runs in, as Talos' own platform
#: code reports it (each platform's `Mode()` under
#: `internal/app/machined/pkg/runtime/v1alpha1/platform/`). A shape looks its
#: mode up by the platform its image is built for, so a node moved to a
#: platform missing here fails by name rather than validating in a mode it no
#: longer runs in.
PLATFORM_MODES: dict[str, Mode] = {'oracle': 'cloud', 'nocloud': 'cloud'}


@dataclass(frozen=True)
class Shape:
    """One node shape: its role, the platform it boots, and the inputs `talos.patches` renders it from."""

    role: talos.Role
    platform: str
    inputs: dict[str, Any] = field(default_factory=dict[str, Any])

    @property
    def mode(self) -> Mode:
        return PLATFORM_MODES[self.platform]


def volume_on(*, dedicated_vip: bool) -> str:
    """The name of a census volume on the dedicated-VIP node, or on some other node.

    Read from `conventions.NODE_VOLUMES` so that the names validated are the
    ones the fleet renders; a census with no such row fails here by name.
    """
    names = [
        name
        for name, entry in sorted(conventions.NODE_VOLUMES.items())
        if (entry.attached_node == conventions.DEDICATED_VIP_NODE) == dedicated_vip
    ]
    assert names, f'no node volume is {"on" if dedicated_vip else "off"} the dedicated-VIP node'
    return names[0]


#: Every node shape the component renders, with the inputs the physical stack
#: gives it. A plain control plane is a cloud node with nothing of its own; a
#: control plane can carry a node volume; the dedicated-VIP control plane
#: carries the secondary private IP the cloud assigns in the VCN, and, in the
#: day-1 rendering it is applied, the volume that follows the VIP as well;
#: the homelab worker states its own address and takes BGP from its gateway.
SHAPES: dict[str, Shape] = {
    'control-plane': Shape('controlplane', image.CLOUD_PLATFORM),
    'control-plane-volume': Shape('controlplane', image.CLOUD_PLATFORM, {'volume': volume_on(dedicated_vip=False)}),
    'dedicated-vip': Shape('controlplane', image.CLOUD_PLATFORM, {'secondary_address': str(conventions.VCN_CIDR[42])}),
    'dedicated-vip-volume': Shape(
        'controlplane',
        image.CLOUD_PLATFORM,
        {'secondary_address': str(conventions.VCN_CIDR[42]), 'volume': volume_on(dedicated_vip=True)},
    ),
    'homelab-worker': Shape(
        'worker',
        image.HOMELAB_PLATFORM,
        {
            'static_address': talos.STATIC_ADDRESSES[conventions.HOMELAB_NODE],
            'bgp_peer': f'{conventions.CLUSTER_VLAN.require_gateway()}/32',
        },
    ),
}

#: The one bound a `talosctl` call runs under. Each call takes well under a
#: second; this is a hang guard, an order of magnitude above that.
TIMEOUT = 60


def pinned_release() -> str:
    """The fleet's Talos release, as `Pulumi.yaml` pins it."""
    return str(yaml.safe_load((ROOT / 'Pulumi.yaml').read_text())['config'][TALOS_KEY])


def run(argv: Sequence[str], cwd: Path) -> str:
    """Run one `talosctl` command; a non-zero exit fails the case with what Talos said."""
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    if proc.returncode != 0:
        pytest.fail(f'`{" ".join(argv)}` exited {proc.returncode}:\n{proc.stderr.strip() or proc.stdout.strip()}')
    return proc.stdout


@pytest.fixture(scope='module')
def talosctl() -> str:
    """The pinned `talosctl`, refused if it is missing or is some other release."""
    binary = pinned_tools.located(TOOL)
    reported = subprocess.run(
        [binary, 'version', '--client', '--short'], capture_output=True, text=True, timeout=TIMEOUT, check=True
    ).stdout
    release = pinned_release()
    found = re.search(r'Talos (v\S+)', reported)
    assert found is not None, f'{binary} reported no release: {reported!r}'
    assert found[1] == release, (
        f'{binary} is Talos {found[1]} and the fleet runs {release}: '
        f'the suite runs under `mise x` after `mise install`, and mise.toml pins the fleet release'
    )
    return binary


@pytest.fixture(scope='module')
def secrets(talosctl: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One secrets bundle every shape is rendered from, as the provider's `Secrets` resource is."""
    bundle = tmp_path_factory.mktemp('talos') / 'secrets.yaml'
    run([talosctl, 'gen', 'secrets', '--talos-version', pinned_release(), '--output-file', str(bundle)], bundle.parent)
    return bundle


def secretbox_secret(bundle: Path) -> str:
    """The generated key the component states in a control plane's patch."""
    document = cast('dict[str, Any]', yaml.safe_load(bundle.read_text()))
    return str(document['secrets']['secretboxencryptionsecret'])


def patches(shape: Shape, bundle: Path) -> list[str]:
    """The patch list the component gives one node shape, as the provider receives it."""
    return talos.patches(
        role=shape.role,
        cert_sans=[BALANCER],
        secretbox_secret=secretbox_secret(bundle) if shape.role == 'controlplane' else None,
        **shape.inputs,
    )


def render(talosctl: str, bundle: Path, shape: Shape, patches: Sequence[str], directory: Path) -> Path:
    """One node's machine configuration, generated and patched as the provider does it."""
    flags: list[str] = []
    for index, patch in enumerate(patches):
        path = directory / f'patch-{index}.json'
        _ = path.write_text(patch)
        flags += ['--config-patch', f'@{path}']
    output = directory / f'{shape.role}.yaml'
    run(
        [
            talosctl,
            'gen',
            'config',
            conventions.CLUSTER_NAME,
            f'https://{BALANCER}:{conventions.MANAGEMENT_PORTS.kubernetes}',
            '--talos-version',
            pinned_release(),
            '--with-secrets',
            str(bundle),
            '--output-types',
            shape.role,
            '--with-docs=false',
            '--with-examples=false',
            *flags,
            '--output',
            str(output),
        ],
        directory,
    )
    return output


def identity(document: dict[str, Any]) -> tuple[str, str | None]:
    """Which rendered document a patch lands in.

    A patch of its own kind is appended as a document of that kind, which a
    `name` tells apart from others of the same kind where it has one; a patch
    with no kind is a strategic merge into the `v1alpha1` document.
    """
    if 'kind' not in document:
        return ('v1alpha1', None)
    name = document.get('name')
    return (str(document['kind']), None if name is None else str(name))


def missing(patch: object, rendered: object, path: str = '') -> list[str]:
    """The paths of what `patch` sets that `rendered` does not carry.

    A mapping is contained key by key, a list item by item -- each item of the
    patch present somewhere in the rendered list, which holds whether Talos
    appended the patch's list to the base's or replaced the base's with it --
    and a scalar by equality.
    """
    if isinstance(patch, dict):
        if not isinstance(rendered, dict):
            return [path or '.']
        found = cast('dict[str, object]', rendered)
        lost: list[str] = []
        for key, value in cast('dict[str, object]', patch).items():
            at = f'{path}.{key}'
            lost += [at] if key not in found else missing(value, found[key], at)
        return lost
    if isinstance(patch, list):
        if not isinstance(rendered, list):
            return [path]
        items = cast('list[object]', rendered)
        return [
            f'{path}[{index}]'
            for index, item in enumerate(cast('list[object]', patch))
            if not any(missing(item, candidate) == [] for candidate in items)
        ]
    return [] if patch == rendered else [path]


@pytest.mark.parametrize('name', list(SHAPES))
def test_talos_accepts_every_node_shape(name: str, talosctl: str, secrets: Path, tmp_path: Path) -> None:
    shape = SHAPES[name]
    applied = patches(shape, secrets)
    configuration = render(talosctl, secrets, shape, applied, tmp_path)
    # Every patch landed: a configuration that silently dropped one would
    # validate while describing some other node.
    rendered = {
        identity(document): document
        for document in cast('list[dict[str, Any] | None]', list(yaml.safe_load_all(configuration.read_text())))
        if document
    }
    for patch in applied:
        document = cast('dict[str, Any]', json.loads(patch))
        key = identity(document)
        assert key in rendered, f'{key} is not in the rendered configuration'
        lost = missing(document, rendered[key])
        assert lost == [], f'{key} does not carry what its patch sets: {lost}'
    run([talosctl, 'validate', '--config', str(configuration), '--mode', shape.mode, '--strict'], tmp_path)


def test_talosctl_is_the_fleets_release() -> None:
    """The verdict above is the pinned release's only while the two pins agree.

    `mise.toml`'s talosctl is what validates; `versions:talos` is what every
    node boots. A validator a minor behind the fleet would accept a document
    the fleet's release refuses, or the other way round.
    """
    tools = tomllib.loads((ROOT / 'mise.toml').read_text())['tools']
    assert f'v{tools[TOOL]}' == pinned_release()


def matches(pattern: str, value: str) -> bool:
    """Whether one renovate match pattern -- a `/regex/`, a glob or a literal -- matches `value`.

    A negated pattern (`!…`) is taken as matching: whether it does depends on
    the rest of its list, and the question here is only whether a rule might.
    """
    if pattern.startswith('!'):
        return True
    if len(pattern) > 1 and pattern.startswith('/') and pattern.endswith('/'):
        return re.search(pattern[1:-1], value) is not None
    return fnmatch.fnmatchcase(value, pattern)


def reaches(rule: str, presents: dict[str, tuple[str, ...]]) -> bool:
    """Whether a rule can match a dependency presenting these values to its matchers.

    A matcher the dependency presents no value for -- an update type, any
    `exclude…` key -- is taken as matching, so a rule is only cleared by a
    matcher that provably misses.
    """
    for key in re.findall(r'^\s*((?:match|exclude)\w+):', rule, re.MULTILINE):
        values = presents.get(key)
        patterns = listed(rule, key)
        # A key whose list this scan cannot read is taken as matching too.
        if values is None or not patterns:
            continue
        if not any(matches(pattern, value) for pattern in patterns for value in values):
            return False
    return True


def test_renovate_moves_both_pins_in_one_pull_request() -> None:
    """The pair the test above holds equal has to move together, from one list of releases.

    Two managers read them: the custom manager for `Pulumi.yaml` reads
    `versions:talos`, and the mise manager reads `mise.toml`. The rule that
    groups the Talos release has to name both dependencies, so one pull
    request carries both halves under one approval, and it has to sit after
    the rule grouping the whole mise manager as the toolchain, since the last
    matching rule wins a field, and no rule after it that sets a group may be
    able to match either half, by dependency, package, manager, file or data
    source. It names the data source both halves read, so neither sees a
    release the other does not. Nothing goes red on a pull
    request that breaks this in renovate itself -- renovate reads its
    configuration from the default branch -- so it is held here.

    Held as text, because there is no JSON5 parser here.
    """
    config = (ROOT / 'renovate.json5').read_text()

    (manager,) = [
        entry
        for entry in re.findall(r'\{[^{}]*\}', config[config.index('customManagers: [') :])
        if '/^Pulumi\\\\.yaml$/' in entry and 'versions:talos' in entry
    ]
    depname = scalar(manager, 'depNameTemplate')
    datasource = scalar(manager, 'datasourceTemplate')
    assert depname is not None
    assert datasource is not None

    rules = package_rules(config)
    (toolchain,) = [
        index
        for index, rule in enumerate(rules)
        if listed(rule, 'matchManagers') == ['mise'] and not listed(rule, 'matchDepNames')
    ]
    (fleet,) = [index for index, rule in enumerate(rules) if depname in listed(rule, 'matchDepNames')]
    rule = rules[fleet]
    assert toolchain < fleet
    assert sorted(listed(rule, 'matchDepNames')) == sorted([depname, TOOL])
    assert [key for key in re.findall(r'^\s*(\w+):', rule, re.MULTILINE) if key.startswith(('match', 'exclude'))] == [
        'matchDepNames'
    ]
    assert scalar(rule, 'overrideDatasource') == datasource
    assert scalar(rule, 'groupSlug') is not None
    # No later rule that sets a group can match either half, by any matcher.
    halves = {
        depname: {
            'matchDepNames': (depname,),
            'matchPackageNames': (depname,),
            'matchManagers': ('custom.regex', 'regex'),
            'matchFileNames': ('Pulumi.yaml',),
            'matchDatasources': (datasource,),
        },
        # Renovate's mise manager resolves `talosctl` to that same repository.
        TOOL: {
            'matchDepNames': (TOOL,),
            'matchPackageNames': (depname,),
            'matchManagers': ('mise',),
            'matchFileNames': ('mise.toml',),
            'matchDatasources': (datasource,),
        },
    }
    for later in rules[fleet + 1 :]:
        if scalar(later, 'groupName') is None and scalar(later, 'groupSlug') is None:
            continue
        for half, presents in halves.items():
            assert not reaches(later, presents), f'a later rule regroups {half}:\n{later}'
