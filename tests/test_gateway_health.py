"""`gateway-health`, run against a stand-in device: what it reads, what it reports, and that it writes nothing.

The executable is rendered from the declarations the stack program builds the
gateway out of, and run as the homelab host runs it: no argument for the
reading, `--certificates` for the list a LAN probe works from. Its systemd is
`tests/fake_systemd.py`, behind a `systemctl` that answers `--version` itself
with the release a case sets. `machinectl`, which names a running machine's
leader, `nsenter`, which runs `zerotier-cli info` in the overlay member's
leader's mount namespace and `ip -o link show host0` in a bridged machine's
leader's network namespace, the package script, whose `--check` the reading
relays, and `ip`, which lists the policy rules and the device's links, are
stand-ins that answer what a case sets and record what they were asked.
`networkctl` and `systemd-analyze` are the stand-in systemd's own: the links it
reports are the ones a case set, and the configuration it assembles is the
files under the stand-in's tree. The base is read under that tree too: its
`/etc/debian_version` and its `/bin/sh` are files there.

What it reads it reads against the declaration, so the wiring of the
declaration into the program -- which machines, which units -- is
`test_gateway`'s, which renders it through the component under the mock
monitor; here the program is rendered from the same declarations directly.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import device_shell
import process_sessions
import pytest
from fake_systemd import NETWORKCTL, SYSTEMD_ANALYZE, FakeSystemd
from gateway_services import caddy, pin

from kluster import conventions
from kluster.components import gateway
from kluster.components.gateway import container, nspawn, persistence, routing

#: The playbook section every finding points into.
PLAYBOOK_DOC = Path(__file__).parent.parent / 'docs' / 'physical' / 'gateway.md'

#: One finding, as the executable prints it.
FINDING = re.compile(r'^(?P<subject>[^:]+): (?P<state>.+) — playbook: physical/gateway\.md §3, "(?P<title>[^"]+)"$')

#: What `zerotier-cli info` prints for a member that reached its roots, for one that reached
#: them only over the TCP relay, and for one that did not.
ONLINE = '200 info 0123456789 1.14.2 ONLINE'
TUNNELED = '200 info 0123456789 1.14.2 TUNNELED'
OFFLINE = '200 info 0123456789 1.14.2 OFFLINE'

RESOLVERS = tuple(
    container.ResolverService(service=resolver, pin=pin(resolver.name)) for resolver in conventions.gateway.RESOLVERS
)
OVERLAY = container.OverlayDaemon(service=conventions.gateway.OVERLAY, pin=pin(conventions.gateway.OVERLAY.name))
#: The process id the stand-in `machinectl` gives each running machine's leader.
LEADERS = {
    declaration.service.name: str(4242 + offset) for offset, declaration in enumerate((caddy(), *RESOLVERS, OVERLAY))
}
#: The machines on a bridge, as the declaration names them, and the bridge each attaches to.
BRIDGED = {declaration.service.name: declaration.bridge for declaration in (caddy(), *RESOLVERS)}

#: Where under the device the stand-in systemd keeps its own records.
SYSTEMD = 'systemd'

MACHINE_UNITS = tuple(declaration.unit_name for declaration in (caddy(), *RESOLVERS, OVERLAY))

#: The policy rules of a healthy gateway, in the form `ip rule show` prints
#: them: the firmware's rule that looks up the main table, and no kernel-default
#: `32766` one, which the device does not hold.
RULES = '0:\tfrom all lookup local\n32000:\tfrom all lookup main\n'
#: The same rules after networkd removed what it did not create, which leaves
#: the kernel's own.
RULES_WITHOUT_MAIN = '0:\tfrom all lookup local\n'

#: A port one of the machines attaches with, as nspawn names it.
PORT = 'vb-caddy'

#: The longest name a network interface may have (IFNAMSIZ less its terminator).
IFNAME_MAX = 15


def port_name(machine: str) -> str:
    """The host end of a bridged machine's pair, named the way nspawn names it.

    `vb-` and the machine's name, and where that is longer than an interface
    name may be, shortened with a hash of the whole in its last characters
    (systemd.net-naming-scheme(7), `v245`). The hash here is a fixed stand-in:
    the reading never derives the name, so its exact value is nobody's to match.
    """
    name = f'vb-{machine}'
    return name if len(name) <= IFNAME_MAX else f'{name[: IFNAME_MAX - 4]}a1b2'


@dataclass
class _Port:
    """The host end of a bridged machine's pair: its link index on the device, its name, and its master."""

    index: int
    name: str
    master: str | None
    #: Whether the device lists the port among its links; the machine's `host0` names it either way.
    listed: bool = True


#: The address family the reading reads the rules of first.
FAMILY = gateway.POLICY_RULE_FAMILIES[0]


def playbook_titles() -> set[str]:
    """The bullet titles of gateway.md §3, which is where every playbook a finding names lives."""
    text = PLAYBOOK_DOC.read_text()
    section = text[text.index('\n## 3. ') : text.index('\n## 4. ')]
    return set(re.findall(r'^-   \*\*(.+?)\*\* — trigger', section, re.MULTILINE))


@dataclass
class _Device:
    """A stand-in device, the executable rendered for it, and the logs of what its stand-ins were asked."""

    root: Path
    systemd: FakeSystemd
    #: Where the stand-ins that go ahead of the stand-in systemd's own tools live.
    tools: Path
    program: Path
    #: What the overlay member's `zerotier-cli info` prints.
    member: Path
    #: What `10-packages.sh --check` prints; absent, the check passes.
    check_fails: Path
    #: The release `systemctl --version` names.
    systemd_release: Path
    #: What `ip -<family> rule show` prints, one file per family.
    rules: Path
    #: The directories networkd's configuration is assembled from, most specific first.
    etc_networkd: Path
    #: The host end of each bridged machine's pair, by machine name.
    ports: dict[str, _Port]
    #: What `ip -o link show` prints on the device.
    links: Path
    #: Present, `ip -o link show` on the device fails with what it holds.
    links_fail: Path
    #: What `ip -o link show host0` prints in each leader's network namespace, one file per leader, and
    #: beside each a `<leader>.warn` it writes to stderr first, where a case sets one.
    netns: Path
    #: Machines the stand-in `machinectl` no longer knows, though their units still read active: a
    #: machine that stopped between the two questions, one file per machine.
    gone: Path
    machinectl_calls: Path
    nsenter_calls: Path
    check_calls: Path
    ip_calls: Path


def _stub(path: Path, body: str) -> None:
    _ = path.write_text(f'#!/bin/sh\n{body}')
    path.chmod(0o755)


def _systemd_release(*, terminate_signals_nspawn: bool) -> int:
    """A systemd release on the given side of the one `machinectl terminate` changes at, next to it."""
    return gateway.SYSTEMD_TERMINATE_RELEASE - (0 if terminate_signals_nspawn else 1)


def _link_bin_sh(root: Path, target: str) -> None:
    """The stand-in's `/bin/sh` made a link to `target`, an absolute path on the device, as Debian links it."""
    link = root / 'bin' / 'sh'
    shell = root / target.lstrip('/')
    shell.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.symlink_to(os.path.relpath(shell, link.parent))


def _write_ports(device: _Device) -> None:
    """The device's links and each bridged machine's end of its pair, as `ip -o link show` prints them.

    The device's own links come first, the firmware's bridges among them; each
    port follows with its master, if it has one, and each machine's `host0`
    names its port's index as its peer.
    """
    lines = [
        '1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN mode DEFAULT group default qlen 1000'
        '\\    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00',
        *(
            f'{index}: {bridge}: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP mode DEFAULT '
            f'group default qlen 1000\\    link/ether 02:00:00:00:00:0{index} brd ff:ff:ff:ff:ff:ff'
            for index, bridge in enumerate(sorted({'br0', *BRIDGED.values()}), start=2)
        ),
    ]
    for machine, port in device.ports.items():
        master = '' if port.master is None else f'master {port.master} '
        if port.listed:
            lines.append(
                f'{port.index}: {port.name}@if2: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue {master}'
                f'state UP mode DEFAULT group default qlen 1000\\    link/ether 02:00:00:00:01:{port.index:02x} '
                'brd ff:ff:ff:ff:ff:ff link-netnsid 0'
            )
        _ = (device.netns / LEADERS[machine]).write_text(
            f'2: host0@if{port.index}: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP mode '
            'DEFAULT group default qlen 1000\\    link/ether 02:00:00:00:02:00 brd ff:ff:ff:ff:ff:ff '
            'link-netnsid 0\n'
        )
    _ = device.links.write_text(''.join(f'{line}\n' for line in lines))


def _device(tmp_path: Path, *, legacy: tuple[conventions.gateway.LegacyVhost, ...] | None = None) -> _Device:
    """A device every declared machine and unit is healthy on, on the declared base, before a case breaks one thing."""
    root = tmp_path.resolve() / 'device'
    logs = tmp_path / 'logs'
    tools = tmp_path / 'tools'
    live = root / 'etc-systemd-system'
    etc_networkd = root / 'etc' / 'systemd' / 'networkd.conf.d'
    for directory in (
        root,
        logs,
        tools,
        live,
        root / 'etc',
        root / 'bin',
        root / 'rules',
        root / 'netns',
        root / 'gone',
        etc_networkd,
    ):
        directory.mkdir(parents=True)
    systemd = FakeSystemd(
        root / SYSTEMD, unit_path=(live,), config_path=(root / 'etc', root / 'run', root / 'usr' / 'lib')
    )
    device = _Device(
        root=root,
        systemd=systemd,
        tools=tools,
        program=root / gateway.HEALTH_PROGRAM,
        member=root / 'member-info',
        check_fails=root / 'check-fails',
        systemd_release=root / 'systemd-release',
        rules=root / 'rules',
        etc_networkd=etc_networkd,
        ports={
            machine: _Port(index=10 + offset, name=port_name(machine), master=bridge)
            for offset, (machine, bridge) in enumerate(BRIDGED.items())
        },
        links=root / 'links',
        links_fail=root / 'links-fail',
        netns=root / 'netns',
        gone=root / 'gone',
        machinectl_calls=logs / 'machinectl',
        nsenter_calls=logs / 'nsenter',
        check_calls=logs / 'packages',
        ip_calls=logs / 'ip',
    )
    # `machinectl` as systemd 247 answers for a machine whose init is not
    # systemd: `show` names the leader of a running machine and knows no other,
    # and `shell` is refused either way, since machined starts its command
    # through the guest's systemd over the guest's bus, which these machines do
    # not run. Any other verb is refused as one the stand-in does not model.
    _stub(
        Path(systemd.tools) / 'machinectl',
        f'echo "$*" >>{device.machinectl_calls}\n'
        'verb= machine= property= value=\n'
        'for argument; do\n'
        '    case $argument in\n'
        '    --property=*) property=${argument#--property=} ;;\n'
        '    --value) value=1 ;;\n'
        '    -*) ;;\n'
        '    *) if [ -z "$verb" ]; then verb=$argument; elif [ -z "$machine" ]; then machine=$argument; fi ;;\n'
        '    esac\n'
        'done\n'
        f'running() {{ [ ! -e {root / "gone"}/"$machine" ] && systemctl is-active --quiet "systemd-nspawn@$machine.service"; }}\n'
        'case $verb in\n'
        'show)\n'
        '    if [ "$property" != Leader ] || [ -z "$value" ]; then\n'
        '        echo "stand-in machinectl: show is modeled for --property=Leader --value only" >&2; exit 1\n'
        '    fi\n'
        '    running || { echo "Could not get path to machine: No machine \'$machine\' known" >&2; exit 1; }\n'
        '    case $machine in\n'
        + ''.join(f'    {machine}) echo {leader} ;;\n' for machine, leader in LEADERS.items())
        + '    esac ;;\n'
        'shell)\n'
        '    running || { echo "Failed to get shell PTY: No machine \'$machine\' known" >&2; exit 1; }\n'
        '    echo "Failed to get shell PTY: There is no system bus in container $machine." >&2; exit 1 ;;\n'
        '*) echo "stand-in machinectl: the verb $verb is not modeled" >&2; exit 1 ;;\n'
        'esac\n',
    )
    # `nsenter --target <pid> --mount -- <command>`, into the overlay member's
    # leader: there the overlay image's client answers its `info` with what the
    # case set. `nsenter --target <pid> --net -- ip -o link show host0`, into
    # any leader: the `host0` its network namespace holds, which one in the
    # host's namespace holds none of, as `ip` says of a device it does not
    # have. Nothing else is modeled.
    overlay_leader = LEADERS[OVERLAY.service.name]
    _stub(
        Path(systemd.tools) / 'nsenter',
        f'echo "$*" >>{device.nsenter_calls}\n'
        'target=$2\n'
        'case "$1 $3 $4" in\n'
        '"--target --mount --")\n'
        '    shift 4\n'
        f'    if [ "$target" != {overlay_leader} ] || [ "$*" != "{gateway.ZEROTIER_CLI} info" ]; then\n'
        '        echo "stand-in nsenter: only the overlay member runs a command, its client\'s info, not $*" >&2\n'
        '        exit 1\n'
        '    fi\n'
        f'    cat {device.member} ;;\n'
        '"--target --net --")\n'
        '    shift 4\n'
        '    if [ "$*" != "ip -o link show host0" ]; then\n'
        '        echo "stand-in nsenter: a network namespace is asked for its host0 only, not $*" >&2; exit 1\n'
        '    fi\n'
        f'    [ -e {device.netns}/"$target" ] || {{ echo \'Device "host0" does not exist.\' >&2; exit 1; }}\n'
        f'    [ ! -e {device.netns}/"$target".warn ] || cat {device.netns}/"$target".warn >&2\n'
        f'    cat {device.netns}/"$target" ;;\n'
        '*) echo "stand-in nsenter: modeled for --target <leader> --mount|--net -- <command> only, not $*" >&2; exit 1 ;;\n'
        'esac\n',
    )
    packages = root / persistence.PACKAGES_SCRIPT
    _stub(
        packages,
        f'echo "$*" >>{device.check_calls}\n[ -e {device.check_fails} ] || exit 0\ncat {device.check_fails}\nexit 1\n',
    )
    _ = device.member.write_text(ONLINE)
    # `ip -<family> rule show`, which prints the rules a case set for that
    # family, and `ip -o link show`, which prints the device's links; nothing
    # else is modeled.
    _stub(
        tools / 'ip',
        f'echo "$*" >>{device.ip_calls}\n'
        'case "$*" in\n'
        f'-?" rule show") cat {device.rules}/"${{1#-}}" ;;\n'
        f'"-o link show") [ ! -e {device.links_fail} ] || {{ cat {device.links_fail} >&2; exit 1; }}; cat {device.links} ;;\n'
        '*) echo "stand-in ip: modeled for -<family> rule show and -o link show only, not $*" >&2; exit 1 ;;\n'
        'esac\n',
    )
    _write_ports(device)
    for family in gateway.POLICY_RULE_FAMILIES:
        _ = (device.rules / family).write_text(RULES)
    # `systemctl --version` as Debian's systemd answers it, the release first;
    # every other question goes to the stand-in systemd.
    _stub(
        tools / 'systemctl',
        f'if [ "$*" = --version ]; then\n'
        f'    release=$(cat {device.systemd_release})\n'
        '    echo "systemd $release ($release.1-1)"\n'
        '    echo "+PAM +AUDIT +SELINUX"\n'
        '    exit 0\n'
        'fi\n'
        f'exec {Path(systemd.tools) / "systemctl"} "$@"\n',
    )
    base = gateway.DEVICE_BASE
    _ = (root / 'etc' / 'debian_version').write_text(f'{base.debian_release}.11\n')
    _link_bin_sh(root, base.shell)
    _ = device.systemd_release.write_text(str(_systemd_release(terminate_signals_nspawn=base.terminate_signals_nspawn)))

    proxy = caddy() if legacy is None else caddy(legacy)
    _ = device.program.write_text(
        gateway.health_program(
            caddy=proxy, resolvers=RESOLVERS, overlay_daemon=OVERLAY, packages_script=str(packages), root=str(root)
        )
    )
    device.program.chmod(0o755)

    for unit in gateway.DELIVERED_UNITS:
        _ = (live / unit).write_text(
            f'[Unit]\nDescription={unit}\n[Service]\nType=oneshot\n[Install]\nWantedBy=multi-user.target\n'
        )
        systemd.enable(unit)
        systemd.activate(unit)
    systemd.ship('systemd-nspawn@.service')
    for unit in MACHINE_UNITS:
        systemd.activate(unit)
    systemd.ship(routing.FRR_SERVICE)
    systemd.activate(routing.FRR_SERVICE)
    # networkd as the firmware runs it, told what the runtime declares, and
    # managing none of the device's links.
    systemd.ship(persistence.NETWORKD_UNIT)
    systemd.activate(persistence.NETWORKD_UNIT)
    _ = (etc_networkd / nspawn.NETWORKD_DROPIN).write_text(nspawn.networkd_dropin())
    for name, kind in (('lo', 'loopback'), ('eth8', 'ether'), ('br0', 'bridge'), (PORT, 'veth')):
        systemd.link(name, kind=kind)
    _ = systemd.take_calls()
    return device


@dataclass
class _Reading:
    status: int
    lines: list[str]
    errors: str


def _read(device: _Device, *args: str) -> _Reading:
    """Run the executable once, as `ssh gw /data/custom/bin/gateway-health` would."""
    completed = process_sessions.run(
        [*device_shell.argv(gateway.HEALTH_PROGRAM, device.program), *args],
        env={'PATH': f'{device.tools}:{device.systemd.tools}:/usr/bin:/bin'},
        text=True,
        timeout=30,
    )
    return _Reading(status=completed.returncode, lines=completed.stdout.splitlines(), errors=completed.stderr)


def _findings(reading: _Reading) -> list[re.Match[str]]:
    findings = [FINDING.match(line) for line in reading.lines]
    assert all(findings), reading.lines
    return [finding for finding in findings if finding is not None]


def test_a_healthy_device_gives_no_output_and_exit_status_zero(tmp_path: Path) -> None:
    device = _device(tmp_path)

    reading = _read(device)

    assert (reading.status, reading.lines, reading.errors) == (0, [], '')


def _stop_a_machine(device: _Device) -> None:
    device.systemd.stop(RESOLVERS[-1].unit_name)


def _fail_the_boot_chain(device: _Device) -> None:
    device.systemd.failing(persistence.UDM_BOOT_UNIT)
    device.systemd.stop(persistence.UDM_BOOT_UNIT)


def _disable_a_unit(device: _Device) -> None:
    _ = process_sessions.run(
        [os.path.join(device.systemd.tools, 'systemctl'), '--quiet', 'disable', routing.CONVERGER_UNIT],
        timeout=30,
        check=True,
    )


def _stop_a_unit(device: _Device) -> None:
    device.systemd.stop(gateway.access.CONVERGER_UNIT)


def _stop_the_routing_daemon(device: _Device) -> None:
    device.systemd.stop(routing.FRR_SERVICE)


#: A bridged machine whose name is too long for its port to carry it whole, and one whose is not.
SHORTENED = next(machine for machine in BRIDGED if port_name(machine) != f'vb-{machine}')
WHOLE = next(machine for machine in BRIDGED if port_name(machine) == f'vb-{machine}')


def _detach_a_port(device: _Device) -> None:
    device.ports[SHORTENED].master = None
    _write_ports(device)


def _move_a_port_to_another_bridge(device: _Device) -> None:
    device.ports[WHOLE].master = 'br0'
    _write_ports(device)


def _take_the_member_offline(device: _Device) -> None:
    _ = device.member.write_text(OFFLINE)


def _leave_a_member_uncached(device: _Device) -> None:
    _ = device.check_fails.write_text('packages: the offline cache in /data/custom/dpkg holds no deb of rsync\n')


def _move_the_debian_release(device: _Device) -> None:
    _ = (device.root / 'etc' / 'debian_version').write_text(f'{int(gateway.DEVICE_BASE.debian_release) + 1}.1\n')


def _another_shell() -> str:
    """A shell `/bin/sh` could resolve to that is not the declared one."""
    return next(shell for shell in ('/bin/bash', '/bin/mksh') if shell != gateway.DEVICE_BASE.shell)


def _replace_bin_sh(device: _Device) -> None:
    _link_bin_sh(device.root, _another_shell())


def _move_systemd_across_the_terminate_release(device: _Device) -> None:
    moved = not gateway.DEVICE_BASE.terminate_signals_nspawn
    _ = device.systemd_release.write_text(str(_systemd_release(terminate_signals_nspawn=moved)))


def _lose_the_main_rule(device: _Device) -> None:
    _ = (device.rules / FAMILY).write_text(RULES_WITHOUT_MAIN)


def _let_networkd_manage_a_port(device: _Device) -> None:
    device.systemd.link(PORT, kind='veth', setup='configured')


#: One of the options the declared drop-in turns off.
GUARDED = next(iter(nspawn.FOREIGN_MANAGEMENT))


def _turn_an_option_back_on_in_a_later_drop_in(device: _Device) -> None:
    later = f'9{nspawn.NETWORKD_DROPIN}'
    assert later > nspawn.NETWORKD_DROPIN
    _ = (device.etc_networkd / later).write_text(f'[{nspawn.NETWORKD_SECTION}]\n{GUARDED}=yes\n')


@pytest.mark.parametrize(
    ('deviation', 'subject', 'playbook'),
    [
        (_stop_a_machine, RESOLVERS[-1].unit_name, gateway.PLAYBOOKS.machine),
        (_detach_a_port, SHORTENED, gateway.PLAYBOOKS.port),
        (_move_a_port_to_another_bridge, WHOLE, gateway.PLAYBOOKS.port),
        (_fail_the_boot_chain, persistence.UDM_BOOT_UNIT, gateway.PLAYBOOKS.boot_chain),
        (_disable_a_unit, routing.CONVERGER_UNIT, gateway.PLAYBOOKS.unit),
        (_stop_a_unit, gateway.access.CONVERGER_UNIT, gateway.PLAYBOOKS.unit),
        (_stop_the_routing_daemon, routing.FRR_SERVICE, gateway.PLAYBOOKS.routing),
        (_take_the_member_offline, conventions.gateway.OVERLAY.name, gateway.PLAYBOOKS.overlay),
        (_leave_a_member_uncached, f'{persistence.PACKAGES_SCRIPT} --check', gateway.PLAYBOOKS.packages),
        (_move_the_debian_release, '/etc/debian_version', gateway.PLAYBOOKS.base),
        (_replace_bin_sh, '/bin/sh', gateway.PLAYBOOKS.base),
        (_move_systemd_across_the_terminate_release, 'systemd', gateway.PLAYBOOKS.base),
        (_lose_the_main_rule, f'ip -{FAMILY} rule', gateway.PLAYBOOKS.networkd),
        (_let_networkd_manage_a_port, PORT, gateway.PLAYBOOKS.networkd),
        (_turn_an_option_back_on_in_a_later_drop_in, GUARDED, gateway.PLAYBOOKS.networkd),
    ],
    ids=[
        'machine',
        'port-detached',
        'port-elsewhere',
        'boot-chain',
        'unit-disabled',
        'unit-inactive',
        'routing-daemon',
        'member-offline',
        'cache',
        'debian-release',
        'bin-sh',
        'systemd',
        'no-main-rule',
        'managed-link',
        'option-on',
    ],
)
def test_each_deviation_is_exactly_one_finding_that_names_its_subject_and_its_playbook(
    tmp_path: Path, deviation: Callable[[_Device], None], subject: str, playbook: str
) -> None:
    """One thing wrong is one line, and the line says what is wrong and where its playbook is.

    The playbook is named by its title in gateway.md §3, which is where the
    reader of the morning's mail goes next: a title that is not a bullet there
    is a finding with no procedure behind it (architecture.md §4.3).
    """
    device = _device(tmp_path)
    deviation(device)

    reading = _read(device)

    findings = _findings(reading)
    assert reading.status == 1
    assert [(finding['subject'], finding['title']) for finding in findings] == [(subject, playbook)], reading.lines
    assert playbook in playbook_titles()


def test_a_member_on_the_tcp_relay_is_online(tmp_path: Path) -> None:
    """`TUNNELED` is a member that reaches its roots over the relay: reachable, so nothing to report."""
    device = _device(tmp_path)
    _ = device.member.write_text(TUNNELED)

    reading = _read(device)

    assert (reading.status, reading.lines) == (0, [])


def test_a_member_offline_is_reported_with_what_its_client_said(tmp_path: Path) -> None:
    device = _device(tmp_path)
    _take_the_member_offline(device)

    (finding,) = _findings(_read(device))

    assert finding['state'] == f'zerotier-cli info: {OFFLINE}'


@pytest.mark.parametrize(
    ('deviation', 'machine', 'master'),
    [(_detach_a_port, SHORTENED, None), (_move_a_port_to_another_bridge, WHOLE, 'br0')],
    ids=['detached', 'elsewhere'],
)
def test_a_port_off_its_bridge_is_reported_by_the_name_the_device_gives_it(
    tmp_path: Path, deviation: Callable[[_Device], None], machine: str, master: str | None
) -> None:
    """The finding names the machine, and quotes the port as the device names it, which is not always `vb-<machine>`.

    nspawn shortens a port name longer than an interface name may be, so the
    reading finds the port from the machine's own end of the pair; the operator
    then reaches it by the name the finding quotes. Beside it are the bridge
    the declaration names and the master the device reports, where it reports
    one.
    """
    device = _device(tmp_path)
    deviation(device)

    (finding,) = _findings(_read(device))

    assert finding['subject'] == machine
    assert device.ports[machine].name in finding['state']
    assert BRIDGED[machine] in finding['state']
    assert master is None or master in finding['state']


def test_a_port_the_reading_cannot_find_is_a_finding(tmp_path: Path) -> None:
    """A running machine whose end of the pair names no port is reported, not passed over as attached.

    The finding carries what `ip` said in the machine's namespace, which is
    where the operator starts.
    """
    device = _device(tmp_path)
    (device.netns / LEADERS[WHOLE]).unlink()

    (finding,) = _findings(_read(device))

    assert (finding['subject'], finding['title']) == (WHOLE, gateway.PLAYBOOKS.port)
    assert 'Device "host0" does not exist.' in finding['state']


def test_what_a_namespace_writes_to_stderr_is_not_read_as_its_answer(tmp_path: Path) -> None:
    """A warning ahead of the `host0` line leaves the line the answer, so a healthy port stays silent."""
    device = _device(tmp_path)
    _ = (device.netns / f'{LEADERS[WHOLE]}.warn').write_text("nsenter: a warning of the tool's own\n")

    reading = _read(device)

    assert (reading.status, reading.lines) == (0, [])


def test_a_machine_that_stops_between_the_two_questions_is_one_finding(tmp_path: Path) -> None:
    """Its unit still read active, and machined no longer knows it: one finding, carrying machined's refusal."""
    device = _device(tmp_path)
    _ = (device.gone / WHOLE).write_text('')

    (finding,) = _findings(_read(device))

    assert (finding['subject'], finding['title']) == (WHOLE, gateway.PLAYBOOKS.port)
    assert f"No machine '{WHOLE}' known" in finding['state']


def test_a_port_the_device_does_not_list_is_a_finding(tmp_path: Path) -> None:
    """A `host0` that names a link the device does not hold is reported, not passed over as attached."""
    device = _device(tmp_path)
    device.ports[WHOLE].listed = False
    _write_ports(device)

    (finding,) = _findings(_read(device))

    assert (finding['subject'], finding['title']) == (WHOLE, gateway.PLAYBOOKS.port)
    assert str(device.ports[WHOLE].index) in finding['state']


def test_a_device_whose_links_cannot_be_listed_reports_every_bridged_machine(tmp_path: Path) -> None:
    """With no list of the device's links, no port can be read as attached, so each machine on a bridge is a finding.

    Each carries what `ip` said, which separates a listing that failed from a
    port the listing did not hold.
    """
    device = _device(tmp_path)
    refusal = 'Cannot send link request: Operation not permitted'
    _ = device.links_fail.write_text(f'{refusal}\n')

    findings = _findings(_read(device))

    assert {finding['subject'] for finding in findings} == set(BRIDGED)
    assert {finding['title'] for finding in findings} == {gateway.PLAYBOOKS.port}
    assert all(refusal in finding['state'] for finding in findings)


def test_the_cache_finding_carries_the_reasons_the_package_script_gave(tmp_path: Path) -> None:
    device = _device(tmp_path)
    _ = device.check_fails.write_text('packages: first reason\npackages: second reason\n')

    (finding,) = _findings(_read(device))

    assert finding['state'] == 'first reason; second reason'


def test_with_the_settings_files_gone_every_declared_machine_is_a_finding(tmp_path: Path) -> None:
    """A firmware update that does not carry `/etc` across takes the machines' settings files with it.

    Nothing starts a machine whose settings are gone, so every declared machine
    is down, and the device holds no list of them any more. The reading's list
    is the declaration's, rendered into it, so it reports every one; a list read
    off `/etc/systemd/nspawn` would report nothing at the moment it matters.
    """
    device = _device(tmp_path)
    for unit in MACHINE_UNITS:
        device.systemd.stop(unit)

    findings = _findings(_read(device))

    assert {finding['subject'] for finding in findings} == set(MACHINE_UNITS)
    assert {finding['title'] for finding in findings} == {gateway.PLAYBOOKS.machine}


def test_a_point_release_of_the_declared_debian_release_is_no_finding(tmp_path: Path) -> None:
    """The base is the major release, which is what the dash pin follows, so a point release moves nothing."""
    device = _device(tmp_path)
    _ = (device.root / 'etc' / 'debian_version').write_text(f'{gateway.DEVICE_BASE.debian_release}.12\n')

    reading = _read(device)

    assert (reading.status, reading.lines) == (0, [])


@pytest.mark.parametrize(
    ('deviation', 'read'),
    [
        (_move_the_debian_release, f'{int(gateway.DEVICE_BASE.debian_release) + 1}.1'),
        (_replace_bin_sh, _another_shell()),
    ],
    ids=['debian-release', 'bin-sh'],
)
def test_a_base_finding_says_what_the_device_read_on_the_devices_terms(
    tmp_path: Path, deviation: Callable[[_Device], None], read: str
) -> None:
    """What the finding quotes is the device's own path, with the stand-in's tree taken off it."""
    device = _device(tmp_path)
    deviation(device)

    (finding,) = _findings(_read(device))

    assert read in finding['state']
    assert str(device.root) not in finding['state']


@pytest.mark.parametrize('shell', ['/bin/dash', '/usr/bin/dash'], ids=['bin', 'merged-usr'])
def test_the_shell_is_read_against_the_declared_one_wherever_it_lives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: str
) -> None:
    """A base with a merged `/usr` resolves `/bin/sh` outside `/bin`, and that is a declaration like any other."""
    monkeypatch.setattr(gateway, 'DEVICE_BASE', replace(gateway.DEVICE_BASE, shell=shell))
    device = _device(tmp_path)
    assert _read(device).lines == []

    _replace_bin_sh(device)

    (finding,) = _findings(_read(device))
    assert (finding['subject'], finding['title']) == ('/bin/sh', gateway.PLAYBOOKS.base)


@pytest.mark.parametrize('terminate_signals_nspawn', [False, True], ids=['below', 'from'])
def test_systemd_is_read_against_the_declared_side_of_the_terminate_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminate_signals_nspawn: bool
) -> None:
    """Either side can be the declared one: the release next to the boundary on that side passes, and across it is a finding."""
    monkeypatch.setattr(
        gateway, 'DEVICE_BASE', replace(gateway.DEVICE_BASE, terminate_signals_nspawn=terminate_signals_nspawn)
    )
    device = _device(tmp_path)
    assert _read(device).lines == []

    _move_systemd_across_the_terminate_release(device)

    (finding,) = _findings(_read(device))
    assert (finding['subject'], finding['title']) == ('systemd', gateway.PLAYBOOKS.base)


def test_a_systemctl_that_names_no_release_is_a_finding(tmp_path: Path) -> None:
    """An answer the reading cannot place on either side of the release is not taken for the declared side."""
    device = _device(tmp_path)
    _ = device.systemd_release.write_text('unknown')

    reading = _read(device)

    (finding,) = _findings(reading)
    assert (finding['subject'], finding['title']) == ('systemd', gateway.PLAYBOOKS.base)
    assert reading.errors == ''


def test_a_networkd_that_is_not_running_is_not_asked_about_its_links(tmp_path: Path) -> None:
    """A daemon that is not running manages nothing, whatever a stale state file says about a link."""
    device = _device(tmp_path)
    device.systemd.stop(persistence.NETWORKD_UNIT)
    _let_networkd_manage_a_port(device)
    _ = device.systemd.take_calls(NETWORKCTL)

    reading = _read(device)

    assert (reading.status, reading.lines) == (0, [])
    assert device.systemd.take_calls(NETWORKCTL) == []


def test_an_option_turned_on_by_an_earlier_drop_in_is_turned_off_by_the_declared_one(tmp_path: Path) -> None:
    """Drop-ins apply in name order and the last assignment wins, so only a later file can undo the declared one."""
    device = _device(tmp_path)
    earlier = f'0{nspawn.NETWORKD_DROPIN}'
    assert earlier < nspawn.NETWORKD_DROPIN
    _ = (device.etc_networkd / earlier).write_text(f'[{nspawn.NETWORKD_SECTION}]\n{GUARDED}=yes\n')

    reading = _read(device)

    assert (reading.status, reading.lines) == (0, [])


def test_an_option_a_later_drop_in_spells_otherwise_is_read_as_networkd_reads_it(tmp_path: Path) -> None:
    """A boolean has more spellings than `yes`, and the finding says what networkd is left with, not the characters."""
    device = _device(tmp_path)
    _ = (device.etc_networkd / f'9{nspawn.NETWORKD_DROPIN}').write_text(f'[{nspawn.NETWORKD_SECTION}]\n{GUARDED}=on\n')

    (finding,) = _findings(_read(device))

    assert finding['subject'] == GUARDED
    assert finding['state'].startswith('reads yes'), finding['state']


@pytest.mark.parametrize('later', [f'{GUARDED}=\n', f'{GUARDED}=maybe\n'], ids=['emptied', 'unparsable'])
def test_an_assignment_networkd_cannot_parse_leaves_the_earlier_value_standing(tmp_path: Path, later: str) -> None:
    """networkd ignores an assignment that does not parse as a boolean, an empty one included.

    So a later drop-in holding one leaves the declared value in force, and the
    reading reports nothing: a finding there would name an exposure networkd
    does not have.
    """
    device = _device(tmp_path)
    _ = (device.etc_networkd / f'9{nspawn.NETWORKD_DROPIN}').write_text(f'[{nspawn.NETWORKD_SECTION}]\n{later}')

    reading = _read(device)

    assert (reading.status, reading.lines) == (0, [])


def test_with_no_networkd_configuration_on_disk_every_guarded_option_is_a_finding(tmp_path: Path) -> None:
    """A firmware update that does not carry `/etc` across takes the drop-in, and every option is back at its default."""
    device = _device(tmp_path)
    (device.etc_networkd / nspawn.NETWORKD_DROPIN).unlink()

    findings = _findings(_read(device))

    assert {finding['subject'] for finding in findings} == set(nspawn.FOREIGN_MANAGEMENT)
    assert {finding['title'] for finding in findings} == {gateway.PLAYBOOKS.networkd}


def _site_blocks(caddyfile: str) -> set[str]:
    """The zone of each site block in a `Caddyfile`: each block is one wildcard certificate."""
    return set(re.findall(r'^\*\.(\S+) \{$', caddyfile, re.MULTILINE))


@pytest.mark.parametrize('legacy', [conventions.gateway.LEGACY_VHOSTS, ()], ids=['legacy-census', 'no-legacy'])
def test_the_certificate_list_is_one_name_per_site_block_at_the_proxys_address(
    tmp_path: Path, legacy: tuple[conventions.gateway.LegacyVhost, ...]
) -> None:
    """A renewal fails per zone, so the probe is per certificate, and a certificate is a site block.

    Each line is the proxy's address and one name, because the site blocks
    refuse a name they do not serve: the probe goes to the address with the
    name as its SNI. Once the legacy census is empty its block is not rendered,
    and the list is the primary zone's alone.
    """
    device = _device(tmp_path, legacy=legacy)
    zones = _site_blocks(container.caddyfile(caddy(legacy)))

    reading = _read(device, '--certificates')

    assert reading.status == 0
    probes = [line.split(' ') for line in reading.lines]
    assert {address for address, _ in probes} == {str(conventions.gateway.CADDY.address)}
    assert sorted(name.split('.', 1)[1] for _, name in probes) == sorted(zones)
    assert len(probes) == (2 if legacy else 1)


def _tree(root: Path) -> dict[str, tuple[int, bytes | None]]:
    """Every path under `root` with its mode and its content, the stand-in systemd's own records left out.

    Those are read through the stand-in instead: the verbs it was asked, and
    the enabled and active sets they leave.
    """
    tree: dict[str, tuple[int, bytes | None]] = {}
    for directory, subdirectories, files in os.walk(root):
        if Path(directory) == root:
            subdirectories.remove(SYSTEMD)
        for name in (*subdirectories, *files):
            path = Path(directory, name)
            stat = path.lstat()
            tree[str(path.relative_to(root))] = (stat.st_mode, path.read_bytes() if path.is_file() else None)
    return tree


def _healthy(_: _Device) -> None:
    pass


@pytest.mark.parametrize(
    'deviation',
    [
        _healthy,
        _stop_a_machine,
        _detach_a_port,
        _fail_the_boot_chain,
        _take_the_member_offline,
        _leave_a_member_uncached,
        _lose_the_main_rule,
        _let_networkd_manage_a_port,
        _turn_an_option_back_on_in_a_later_drop_in,
    ],
)
def test_the_reading_writes_nothing(tmp_path: Path, deviation: Callable[[_Device], None]) -> None:
    """Healthy or not, a reading asks and changes nothing: no systemctl verb that acts, no file touched.

    The package script is asked only for `--check`, the mode that writes
    nothing (its own suite holds that), the overlay member only for
    `zerotier-cli info`, run in its leader's mount namespace, and a bridged
    machine only to show its `host0`, in its leader's network namespace.
    networkd is asked only to list its links and to assemble its configuration,
    and the kernel only to list its rules and its links.
    """
    device = _device(tmp_path)
    deviation(device)
    before = (_tree(device.root), device.systemd.enabled, device.systemd.active)
    _ = device.systemd.take_calls()

    _ = _read(device)

    assert {call.split()[0] for call in device.systemd.take_calls()} <= {'is-active', 'is-enabled'}
    assert device.check_calls.read_text().splitlines() == ['--check']
    assert {call.split()[0] for call in device.machinectl_calls.read_text().splitlines()} == {'show'}
    assert {call.split(' -- ', 1)[1] for call in device.nsenter_calls.read_text().splitlines()} == {
        f'{gateway.ZEROTIER_CLI} info',
        'ip -o link show host0',
    }
    assert {call.split(' ', 1)[1] for call in device.ip_calls.read_text().splitlines()} == {'rule show', 'link show'}
    assert {call.split()[0] for call in device.systemd.take_calls(NETWORKCTL)} == {'list'}
    assert {call.split()[-2] for call in device.systemd.take_calls(SYSTEMD_ANALYZE)} == {'cat-config'}
    assert (_tree(device.root), device.systemd.enabled, device.systemd.active) == before


@pytest.mark.parametrize('args', [('--check',), ('--certificates', 'extra')])
def test_an_argument_it_does_not_take_reads_nothing(tmp_path: Path, args: tuple[str, ...]) -> None:
    """A mistyped mode is refused on stderr rather than taken for the reading, so its exit status is not a verdict."""
    device = _device(tmp_path)

    reading = _read(device, *args)

    assert (reading.status, reading.lines) == (2, [])
    assert 'usage' in reading.errors
    assert device.systemd.take_calls() == []
    assert not device.check_calls.exists()
