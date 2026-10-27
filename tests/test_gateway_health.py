"""`gateway-health`, run against a stand-in device: what it reads, what it reports, and that it writes nothing.

The executable is rendered from the declarations the stack program builds the
gateway out of, and run as the homelab host runs it: no argument for the
reading, `--certificates` for the list a LAN probe works from. Its systemd is
`tests/fake_systemd.py`. `machinectl`, which names the overlay member's
leader, `nsenter`, which runs `zerotier-cli info` in that leader's mount
namespace, and the package script, whose `--check` the reading relays, are
stand-ins that answer what a case sets and record what they were asked.

What it reads it reads against the declaration, so the wiring of the
declaration into the program -- which machines, which units -- is
`test_gateway`'s, which renders it through the component under the mock
monitor; here the program is rendered from the same declarations directly.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import device_shell
import process_sessions
import pytest
from fake_systemd import FakeSystemd
from gateway_services import caddy, pin

from kluster import conventions
from kluster.components import gateway
from kluster.components.gateway import container, persistence, routing

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
#: The process id the stand-in `machinectl` gives a running machine's leader.
LEADER = '4242'

#: Where under the device the stand-in systemd keeps its own records.
SYSTEMD = 'systemd'

MACHINE_UNITS = tuple(declaration.unit_name for declaration in (caddy(), *RESOLVERS, OVERLAY))


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
    program: Path
    #: What the overlay member's `zerotier-cli info` prints.
    member: Path
    #: What `10-packages.sh --check` prints; absent, the check passes.
    check_fails: Path
    machinectl_calls: Path
    nsenter_calls: Path
    check_calls: Path


def _stub(path: Path, body: str) -> None:
    _ = path.write_text(f'#!/bin/sh\n{body}')
    path.chmod(0o755)


def _device(tmp_path: Path, *, legacy: tuple[conventions.gateway.LegacyVhost, ...] | None = None) -> _Device:
    """A device every declared machine and unit is healthy on, before a case breaks one thing."""
    root = tmp_path / 'device'
    logs = tmp_path / 'logs'
    live = root / 'etc-systemd-system'
    for directory in (root, logs, live):
        directory.mkdir()
    systemd = FakeSystemd(root / SYSTEMD, unit_path=(live,))
    device = _Device(
        root=root,
        systemd=systemd,
        program=root / gateway.HEALTH_PROGRAM,
        member=root / 'member-info',
        check_fails=root / 'check-fails',
        machinectl_calls=logs / 'machinectl',
        nsenter_calls=logs / 'nsenter',
        check_calls=logs / 'packages',
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
        'running() { systemctl is-active --quiet "systemd-nspawn@$machine.service"; }\n'
        'case $verb in\n'
        'show)\n'
        '    if [ "$property" != Leader ] || [ -z "$value" ]; then\n'
        '        echo "stand-in machinectl: show is modeled for --property=Leader --value only" >&2; exit 1\n'
        '    fi\n'
        '    running || { echo "Could not get path to machine: No machine \'$machine\' known" >&2; exit 1; }\n'
        f'    echo {LEADER} ;;\n'
        'shell)\n'
        '    running || { echo "Failed to get shell PTY: No machine \'$machine\' known" >&2; exit 1; }\n'
        '    echo "Failed to get shell PTY: There is no system bus in container $machine." >&2; exit 1 ;;\n'
        '*) echo "stand-in machinectl: the verb $verb is not modeled" >&2; exit 1 ;;\n'
        'esac\n',
    )
    # `nsenter --target <pid> --mount -- <command>`, into the one process the
    # stand-in `machinectl` names: there the overlay image's client answers its
    # `info` with what the case set, and nothing else is modeled.
    _stub(
        Path(systemd.tools) / 'nsenter',
        f'echo "$*" >>{device.nsenter_calls}\n'
        f'if [ "$1 $2 $3 $4" != "--target {LEADER} --mount --" ]; then\n'
        '    echo "stand-in nsenter: modeled for --target <leader> --mount -- <command> only, not $*" >&2; exit 1\n'
        'fi\n'
        'shift 4\n'
        f'if [ "$*" != "{gateway.ZEROTIER_CLI} info" ]; then\n'
        '    echo "stand-in nsenter: the machine runs no command but the client\'s info, not $*" >&2; exit 1\n'
        'fi\n'
        f'cat {device.member}\n',
    )
    packages = root / persistence.PACKAGES_SCRIPT
    _stub(
        packages,
        f'echo "$*" >>{device.check_calls}\n[ -e {device.check_fails} ] || exit 0\ncat {device.check_fails}\nexit 1\n',
    )
    _ = device.member.write_text(ONLINE)

    proxy = caddy() if legacy is None else caddy(legacy)
    _ = device.program.write_text(
        gateway.health_program(caddy=proxy, resolvers=RESOLVERS, overlay_daemon=OVERLAY, packages_script=str(packages))
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
        env={'PATH': f'{device.systemd.tools}:/usr/bin:/bin'},
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


def _take_the_member_offline(device: _Device) -> None:
    _ = device.member.write_text(OFFLINE)


def _leave_a_member_uncached(device: _Device) -> None:
    _ = device.check_fails.write_text('packages: the offline cache in /data/custom/dpkg holds no deb of rsync\n')


@pytest.mark.parametrize(
    ('deviation', 'subject', 'playbook'),
    [
        (_stop_a_machine, RESOLVERS[-1].unit_name, gateway.PLAYBOOKS.machine),
        (_fail_the_boot_chain, persistence.UDM_BOOT_UNIT, gateway.PLAYBOOKS.boot_chain),
        (_disable_a_unit, routing.CONVERGER_UNIT, gateway.PLAYBOOKS.unit),
        (_stop_a_unit, gateway.access.CONVERGER_UNIT, gateway.PLAYBOOKS.unit),
        (_stop_the_routing_daemon, routing.FRR_SERVICE, gateway.PLAYBOOKS.routing),
        (_take_the_member_offline, conventions.gateway.OVERLAY.name, gateway.PLAYBOOKS.overlay),
        (_leave_a_member_uncached, f'{persistence.PACKAGES_SCRIPT} --check', gateway.PLAYBOOKS.packages),
    ],
    ids=['machine', 'boot-chain', 'unit-disabled', 'unit-inactive', 'routing-daemon', 'member-offline', 'cache'],
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
    'deviation', [_healthy, _stop_a_machine, _fail_the_boot_chain, _take_the_member_offline, _leave_a_member_uncached]
)
def test_the_reading_writes_nothing(tmp_path: Path, deviation: Callable[[_Device], None]) -> None:
    """Healthy or not, a reading asks and changes nothing: no systemctl verb that acts, no file touched.

    The package script is asked only for `--check`, the mode that writes
    nothing (its own suite holds that), and the overlay member only for
    `zerotier-cli info`, run in its leader's mount namespace.
    """
    device = _device(tmp_path)
    deviation(device)
    before = (_tree(device.root), device.systemd.enabled, device.systemd.active)
    _ = device.systemd.take_calls()

    _ = _read(device)

    assert {call.split()[0] for call in device.systemd.take_calls()} <= {'is-active', 'is-enabled'}
    assert device.check_calls.read_text().splitlines() == ['--check']
    # Every deviation here leaves the overlay member's machine running, so the
    # member is read on each of them: one machine looked up, one read verb run.
    assert [call.split()[0] for call in device.machinectl_calls.read_text().splitlines()] == ['show']
    assert [call.split()[-1] for call in device.nsenter_calls.read_text().splitlines()] == ['info']
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
