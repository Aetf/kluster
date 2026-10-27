"""The overlay probe, as physical/gateway.md §2.4.1 writes it.

The probe is a throwaway Pulumi project the operator writes out of a heredoc in
gateway.md and runs from the checkout root against a scratch ZeroTier network.
Nothing else in the repository holds it, so these cases read its program out of
the document, as `test_unifi_probe` reads the controller probe's, and run it
under the mocks, so no network is touched. A change to the heredoc is a change
to what they run. They hold what the drill's readings rest on:

-   the network is adopted by the id the probe is configured with, and the
    production network's id appears nowhere in a run;
-   the rules are `flow_rules`' own program: every address in them is a
    scratch one, and moved back to production's they are the program
    `flow_rules` composes from the production addresses -- this case's own
    call, with the arguments the `physical` stack passes -- rendered over the
    same minted node addresses, so a hand-written rules string or a program
    composed some other way fails here. That the stack makes that call is
    `test_physical_stack`'s to hold;
-   there is one member per stand-in, at the address and in the role the
    document's stand-in table gives, each written on the identity minted for
    it, and `ci-dns`'s role follows `ciDnsRole`;
-   every range the scratch network carries, its managed DNS servers among
    them, is private in ZeroTier's scope and disjoint from every network the
    installation declares, its DNS block names no production domain, and the
    pools are the component's derivation over the probe's roster and routes:
    the scratch subnet's hosts less every placed address and next hop;
-   each control changes only the lines the document says it does, never a leg,
    and ZeroTier's compiler takes it;
-   every stand-in's identity is exported, as a secret, under its name.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib
import json
import pkgutil
import re
import subprocess
import textwrap
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, summarize_address_range
from pathlib import Path
from typing import Any, cast

import pinned_tools
import process_sessions
import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Declaration, Recorder, declaring, run_with

from kluster import conventions
from kluster.components.overlay.flow_rules import flow_rules
from kluster.conventions.cluster import POD_CIDR_V4, SERVICE_CIDR_V4
from kluster.conventions.site import CONTAINER_VLAN, LAN_POOL, SITE_NETWORKS

DOCUMENT = Path(__file__).parent.parent / 'docs' / 'physical' / 'gateway.md'

#: Where the document puts the probe, relative to the checkout root it runs
#: from (`PROBE=$PWD/.claude/overlay-probe`), and the project's name in it.
PROBE = Path('.claude') / 'overlay-probe'
PROJECT = 'overlay-probe'

#: What the operator sets as `networkId`: the scratch network's id, made up.
SCRATCH_NETWORK_ID = '0123456789abcdef'

NETWORK = 'zerotier:index/network:Network'
MEMBER = 'zerotier:index/member:Member'
IDENTITY = 'zerotier:index/identity:Identity'

#: ZeroTier's private scope (ZeroTierOne `node/InetAddress.cpp`, `ipScope`): a
#: client installs a managed address or route outside it only under
#: `allowGlobal`, which is off by default.
PRIVATE_SCOPE = (IPv4Network('10.0.0.0/8'), IPv4Network('172.16.0.0/12'), IPv4Network('192.168.0.0/16'))

#: The vendored ZeroTier rule compiler `test_flow_rules_compile` runs, and the
#: bound one compile runs under, as there.
COMPILER = Path(__file__).parent / 'vendor' / 'zerotier-rule-compiler' / 'cli.js'
COMPILE_TIMEOUT = 20

#: A dotted-quad address in a rule program, `/32` and all left beside it.
ADDRESS = re.compile(r'(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])')
CODE_SPAN = re.compile(r'`([^`\n]*)`')


def program() -> str:
    """The program the document writes into the probe's directory, as it lands there."""
    match = re.search(
        r"cat > \"\$PROBE/__main__\.py\" <<'EOF'\n(?P<body>.*?)\n[ ]*EOF\n",
        DOCUMENT.read_text(),
        re.DOTALL,
    )
    assert match is not None, f'{DOCUMENT} writes no __main__.py for the overlay probe'
    return textwrap.dedent(match['body']) + '\n'


def stand_in_table() -> dict[str, tuple[IPv4Address, str]]:
    """Each member stand-in the document's table names: its overlay address and its role.

    The table is the one in §2.4.1 whose header names a `Stand-in` column; a
    row whose stand-in is no single name in code is the resolver stand-in,
    which is not a member.
    """
    text = DOCUMENT.read_text()
    section = text[text.index('#### 2.4.1') : text.index('#### 2.4.2')]
    rows = [line for line in section.splitlines() if line.startswith('|')]
    (header_at,) = [i for i, row in enumerate(rows) if '| Stand-in |' in row]
    header = [cell.strip() for cell in rows[header_at].strip('|').split('|')]
    name_at, address_at, role_at = header.index('Stand-in'), header.index('Address'), header.index('Role')
    table: dict[str, tuple[IPv4Address, str]] = {}
    for row in rows[header_at + 2 :]:
        cells = [cell.strip() for cell in row.strip('|').split('|')]
        if len(cells) != len(header):
            break
        name = re.fullmatch(r'`([\w-]+)`', cells[name_at])
        if name is None:
            continue
        table[name[1]] = (IPv4Address(CODE_SPAN.findall(cells[address_at])[0]), CODE_SPAN.findall(cells[role_at])[0])
    assert table, 'the stand-in table names no member'
    return table


def node_id(name: str) -> str:
    """The ten hex digits Central would mint for an identity, made up from its logical name."""
    return hashlib.sha256(name.encode()).hexdigest()[:10]


class Central(Recorder):
    """What Central computes: each identity's node address and key, and the adopted network's id."""

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == IDENTITY:
            return {'identityId': node_id(args.name), 'privateKey': f'{args.name}-secret', 'publicKey': 'public'}
        if args.typ == NETWORK:
            return {'networkId': SCRATCH_NETWORK_ID}
        return {}


@dataclass(frozen=True)
class Run:
    """One run of the probe's program: what it declared, what it exported, and its own globals."""

    central: Central
    exported: dict[str, object]
    namespace: dict[str, Any]

    @property
    def network(self) -> Declaration:
        (network,) = self.central.of_type(NETWORK)
        return network

    @property
    def members(self) -> dict[str, dict[str, Any]]:
        """Each member's inputs, by the name Central shows it under."""
        return {str(member.inputs['name']): member.inputs for member in self.central.of_type(MEMBER)}

    @property
    def rules(self) -> str:
        return str(self.network.inputs['flowRules'])


async def run_probe(central: Central | None = None, **config: str) -> Run:
    """The probe's program, run under the mocks with `config` beside the keys the document sets."""
    central = await run_with(central or Central(), stack='probe', project=PROJECT)
    pulumi.runtime.set_all_config(
        {
            f'{PROJECT}:networkId': SCRATCH_NETWORK_ID,
            f'{PROJECT}:zerotierApiToken': 'not-a-token',
            **{f'{PROJECT}:{key}': value for key, value in config.items()},
        },
        secret_keys=[f'{PROJECT}:zerotierApiToken'],
    )
    exported: dict[str, object] = {}
    namespace: dict[str, Any] = {'__name__': '__main__'}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pulumi, 'export', exported.__setitem__)
        async with declaring():
            exec(compile(program(), str(PROBE / '__main__.py'), 'exec'), namespace)
    return Run(central, exported, namespace)


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def runs() -> dict[str, Run]:
    """The program as configured for each reading: as written, and under each key the controls set."""
    return {
        'composed': await run_probe(),
        'retagged': await run_probe(ciDnsRole='personal'),
        'tags': await run_probe(control='tags'),
        'arp': await run_probe(control='arp'),
    }


def production_networks() -> tuple[IPv4Network, ...]:
    """Every IPv4 network the installation declares, which no scratch range may touch."""
    return (
        conventions.overlay.SUBNET,
        *(network.v4 for network in SITE_NETWORKS),
        LAN_POOL.v4,
        POD_CIDR_V4,
        SERVICE_CIDR_V4,
    )


def ci_nodes(run: Run) -> dict[str, str]:
    """The node address each continuous-integration stand-in was written on."""
    return {name: str(run.members[name]['memberId']) for name in conventions.overlay.CI_MEMBERS}


def compile_rules(text: str, directory: Path) -> subprocess.CompletedProcess[str]:
    """ZeroTier's own compiler over `text`, as Central compiles a `rulesSource`."""
    source = directory / 'flow-rules.zt'
    _ = source.write_text(text)
    return process_sessions.run(
        [pinned_tools.located('node'), str(COMPILER), str(source)],
        text=True,
        timeout=COMPILE_TIMEOUT,
        check=False,
    )


def test_the_network_is_adopted_by_the_configured_id_and_the_production_id_is_in_no_request(
    runs: dict[str, Run],
) -> None:
    """The probe adopts the network the operator made by hand; a slip to the production id would govern the home.

    `Overlay` adopts rather than creates, so the id is the whole of which
    network the run writes routes, rules and members onto.
    """
    composed = runs['composed']

    assert composed.central.options_of(composed.network.name, NETWORK).importId == SCRATCH_NETWORK_ID
    for name, run in runs.items():
        assert conventions.overlay.NETWORK_ID not in str(run.central.requested), name
        assert conventions.overlay.NETWORK_ID not in str([d.inputs for d in run.central.declared]), name


def conventions_now() -> dict[str, dict[str, object]]:
    """Every module of `kluster.conventions` and what each of its names is bound to, by identity."""
    modules = [
        conventions,
        *(importlib.import_module(m.name) for m in pkgutil.iter_modules(conventions.__path__, 'kluster.conventions.')),
    ]
    return {module.__name__: {name: id(value) for name, value in vars(module).items()} for module in modules}


@pytest.mark.asyncio(loop_scope='module')
async def test_the_probe_rebinds_no_convention(runs: dict[str, Run]) -> None:
    """The probe's scratch values reach the component as arguments, never by changing `conventions`.

    A convention rebound for the run would be read by every later lookup in
    the same process, production's routes and DNS beside a scratch subnet.
    """
    before = conventions_now()

    _ = await run_probe()

    assert conventions_now() == before


@pytest.mark.asyncio(loop_scope='module')
async def test_the_production_network_id_is_refused_before_anything_is_declared(runs: dict[str, Run]) -> None:
    """An operator who pastes the production id gets a refusal, not an adoption the teardown would destroy.

    The probe's `up` would write the scratch routes, rules and pools onto the
    adopted network, and its teardown deletes the network it adopted, so the id
    is refused in the program rather than left to a reading of the preview.
    `runs` is taken so that this run comes after the module's own.
    """
    central = Central()

    with pytest.raises(ValueError, match='production network'):
        _ = await run_probe(central, networkId=conventions.overlay.NETWORK_ID.upper())

    assert central.declared == []


def test_the_rules_are_flow_rules_program_over_the_scratch_addresses(runs: dict[str, Run]) -> None:
    """Moved back to production's addresses, the scratch program is what `flow_rules` composes from them.

    The expected side is this case's own `flow_rules(...)` call over the
    production addresses, the arguments `stacks/physical.py` hands it; whether
    the stack still makes that call is `test_physical_stack`'s subject. What
    this holds is that the probe runs the same function and the same render,
    over addresses that differ by one octet, rendered over the node addresses
    the probe minted -- which is what lets a reading on the scratch network
    stand for the program Central will hold.
    """
    composed = runs['composed']
    moves: dict[IPv4Network, IPv4Network] = {
        composed.namespace['SUBNET']: conventions.overlay.SUBNET,
        composed.namespace['LAN']: CONTAINER_VLAN.v4,
    }

    def moved(match: re.Match[str]) -> str:
        address = IPv4Address(match[0])
        for scratch, production in moves.items():
            if address in scratch:
                return str(production.network_address + (int(address) - int(scratch.network_address)))
        raise AssertionError(f'{address} in the scratch rules is in no scratch network')

    stack_program = flow_rules(
        gateway_overlay_address=conventions.overlay.UDM,
        homelab_overlay_address=conventions.overlay.member(conventions.overlay.MEMBER_HOMELAB).address,
        resolver_site_addresses=[resolver.address for resolver in conventions.gateway.RESOLVERS],
    ).render(ci_nodes(composed))

    assert ADDRESS.sub(moved, composed.rules) == stack_program


def test_one_member_per_stand_in_at_the_address_and_role_the_table_gives(runs: dict[str, Run]) -> None:
    """The stand-ins the readings dial are the members the probe declares, and `ciDnsRole` moves one tag.

    Each member is written on the identity minted for it, so the identity a
    stand-in installs is the one the rules name.
    """
    composed, retagged = runs['composed'], runs['retagged']
    identities = {node_id(identity.name) for identity in composed.central.of_type(IDENTITY)}

    def placed(run: Run) -> dict[str, tuple[IPv4Address, int]]:
        found: dict[str, tuple[IPv4Address, int]] = {}
        for name, inputs in run.members.items():
            (address,) = inputs['ipAssignments']
            ((tag, role),) = inputs['tags']
            assert tag == conventions.overlay.TAG_ROLE_ID, name
            assert inputs['memberId'] in identities, name
            found[name] = (IPv4Address(address), int(role))
        return found

    table = {
        name: (address, int(conventions.overlay.Role[role.upper()]))
        for name, (address, role) in stand_in_table().items()
    }

    ci_dns = conventions.overlay.MEMBER_CI_DNS
    assert placed(composed) == table
    assert placed(retagged) == table | {ci_dns: (table[ci_dns][0], int(conventions.overlay.Role.PERSONAL))}


def test_every_scratch_range_is_private_and_clear_of_the_installations_networks(runs: dict[str, Run]) -> None:
    """Nothing the scratch network carries can reach, or be confused with, a network the home uses.

    The pools are the stack's derivation over the probe's roster: every host
    address of the scratch subnet less the stand-ins' addresses and the
    gateway's, so a pool left at production's ranges, or one holding a
    stand-in's address, fails here.
    """
    composed = runs['composed']
    network = composed.network.inputs
    routes = [(IPv4Network(route['target']), route.get('via') or None) for route in network['routes']]
    (subnet,) = [target for target, via in routes if via is None]
    placed = {IPv4Address(address) for inputs in composed.members.values() for address in inputs['ipAssignments']}
    pools = [(IPv4Address(pool['start']), IPv4Address(pool['end'])) for pool in network['assignmentPools']]

    ranges = [target for target, _ in routes]
    ranges += [IPv4Network(address) for address in placed]
    ranges += [block for start, end in pools for block in summarize_address_range(start, end)]
    ranges += [IPv4Network(server) for block in network['dns'] for server in block['servers']]
    # The managed DNS reaches every member that opts in; production's domain
    # on a scratch network would send its names to the scratch resolvers.
    production_domain = conventions.overlay.MANAGED_DNS.domain
    for block in network['dns']:
        assert block['domain'] != production_domain, block
        assert not str(block['domain']).endswith(f'.{production_domain}'), block
    for scratch in ranges:
        assert any(scratch.subnet_of(scope) for scope in PRIVATE_SCOPE), scratch
        assert not any(scratch.overlaps(production) for production in production_networks()), scratch

    assert pools, 'no pool declared: Central would keep the pools it has'
    assert all(start in subnet and end in subnet for start, end in pools)
    assert not any(start <= address <= end for address in placed for start, end in pools)
    pooled = sum(int(end) - int(start) + 1 for start, end in pools)
    assert pooled + len(placed) == subnet.num_addresses - 2


@pytest.mark.parametrize('control', ['tags', 'arp'])
def test_each_control_changes_only_the_drops_it_names_and_compiles(
    runs: dict[str, Run], control: str, tmp_path: Path
) -> None:
    """A control weakens one kind of drop and nothing else, so what it lets through is that drop's work.

    `tags` keys the closing drops on the role tag instead of the node address
    (item 9), and `arp` removes the ARP drops (item 8). A control that also
    touched a leg would make the reading pass or fail for a reason other than
    the drop it is about.
    """
    composed, weakened = runs['composed'], runs[control]
    nodes = set(ci_nodes(composed).values())
    diff = list(difflib.ndiff(composed.rules.splitlines(), weakened.rules.splitlines()))
    removed = [line[2:] for line in diff if line.startswith('- ')]
    added = [line[2:] for line in diff if line.startswith('+ ')]

    drop = r'^drop zt(src|dest) (\w+);$' if control == 'tags' else r'^drop ethertype arp and zt(src|dest) (\w+);$'
    named = [re.fullmatch(drop, line) for line in removed]
    assert all(named), removed
    assert {(m[1], m[2]) for m in named if m} == {(end, node) for end in ('src', 'dest') for node in nodes}
    assert len(removed) == 2 * len(nodes)
    if control == 'tags':
        assert sorted(added) == ['drop treq role ci;', 'drop tseq role ci;']
    else:
        assert added == []

    compiled = compile_rules(weakened.rules, tmp_path)
    assert compiled.returncode == 0, compiled.stderr or compiled.stdout
    assert json.loads(compiled.stdout)['config']['rules']


@pytest.mark.asyncio(loop_scope='module')
async def test_every_stand_ins_identity_is_exported_as_a_secret(runs: dict[str, Run]) -> None:
    """A stand-in installs its identity from the probe's state, and the export keeps it secret.

    A run's identity is what a holder joins with, so an export that lost its
    secret marking would print it in the probe's terminal and state in the
    clear.
    """
    composed = runs['composed']
    minted = {node_id(identity.name): identity.name for identity in composed.central.of_type(IDENTITY)}

    assert set(composed.exported) == set(composed.members)
    for name, value in composed.exported.items():
        output = cast('pulumi.Output[str]', value)
        assert await output.is_secret(), name
        assert await output.future() == f'{minted[composed.members[name]["memberId"]]}-secret', name
