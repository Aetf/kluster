"""The mail relay, declared against mocks, and its configuration run by the pinned image's own exim.

The relay is declared with an image pin, ciphertexts and senders of the
test's own, so what is held is how the component composes what it is handed.
Its configuration is the one the declared ConfigMap carries. The cases that
need exim to read it run the image `Pulumi.yaml` pins, with podman, as the
pod runs it: as its user and group, with every capability dropped, on a
read-only root with the spool on a scratch filesystem, and with the
configuration owned by root, as the kubelet writes a ConfigMap's file. exim's
answers to an SMTP session, and what it does with a queued message, are what
say whom it relays for and how it hands mail on, not a reading of the file.

Those cases run as `pinned_image` has them: skipped where podman is not on
`PATH`, as in a cloud session, and failed on CI's runner, which has podman;
the image pulled by its digest; and every container carrying `--rm`, a
`--timeout` and the module's label (framework/testing.md §1).

Literals written here are the upstream projects' contracts: exim's
configuration options, command-line options and SMTP reply codes, the
Kubernetes and Cilium fields, the kubelet's layout of a Secret's files, the
sealed-secrets controller's scope annotations, and the label Pod Security
Admission reads.
"""

from __future__ import annotations

import io
import ipaddress
import re
import tarfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import process_sessions
import pytest
import pytest_asyncio
import yaml
from mock_monitor import Recorder, declaring, run_with
from pinned_image import COMMAND_TIMEOUT, CONTAINER_TIMEOUT, pinned, pulled, require_podman

from kluster import conventions
from kluster.components.apps.exim import CONFIGURATION, HOST, SMTP, Exim
from kluster.components.cilium.bgp import CONTROL_PLANE_LABEL
from kluster.components.dns import base as dns_base
from kluster.components.dns.record import zone_records
from kluster.lib.k8s import sealed_ciphertext
from kluster.lib.versions import ImagePin

ROOT = Path(__file__).resolve().parents[1]

NAMESPACE_TYPE = 'kubernetes:core/v1:Namespace'
DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
SERVICE = 'kubernetes:core/v1:Service'
CONFIG_MAP = 'kubernetes:core/v1:ConfigMap'
SEALED_SECRET = 'crds:bitnami.com/v1alpha1:SealedSecret'
POLICY = 'crds:cilium.io/v2:CiliumNetworkPolicy'

#: The label Cilium gives every endpoint, naming its pod's namespace.
NAMESPACE_LABEL = 'k8s:io.kubernetes.pod.namespace'

#: The pin the relay is declared with here: a reference that is no published
#: image, so a container carrying it carries the pin.
IMAGE = ImagePin('registry.example.invalid/exim-relay', 'v9.9.9', f'sha256:{"e" * 64}')

#: The ciphertexts it is handed, each naming what it stands for.
SMTP_CIPHERTEXT = {'smtp_user': 'sealed-account', 'smtp_pass': 'sealed-app-password'}
DKIM = {'tls.key': 'sealed-dkim-key'}

#: The namespaces it is handed as its senders.
SENDERS = ('first-sender', 'second-sender')

#: The label a container started here carries, and so the census of what a run
#: has left: `podman ps --all --filter label=kluster-test=test_apps_exim`.
LABEL = f'kluster-test={Path(__file__).stem}'

#: Why these cases need podman.
WHY = 'the configuration is run by the exim of the pinned image with it'

#: The spool, which the pod mounts an `emptyDir` at, every user's to write.
SPOOL = '/var/spool/exim'


async def declared(senders: tuple[str, ...]) -> Recorder:
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = Exim('exim', image=IMAGE, smtp=SMTP_CIPHERTEXT, dkim=DKIM, senders=senders)
    return monitor


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def relay() -> Recorder:
    """The relay, declared with the test's pin, ciphertexts and senders."""
    return await declared(SENDERS)


def the_one(monitor: Recorder, typ: str) -> dict[str, Any]:
    (found,) = [it.inputs for it in monitor.of_type(typ)]
    return found


def sealed_secret(monitor: Recorder, name: str) -> dict[str, Any]:
    (found,) = [it.inputs for it in monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == name]
    return found


def pod_of(monitor: Recorder) -> dict[str, Any]:
    return the_one(monitor, DEPLOYMENT)['spec']['template']


def container_of(monitor: Recorder) -> dict[str, Any]:
    (container,) = pod_of(monitor)['spec']['containers']
    return container


def configuration(monitor: Recorder) -> str:
    """The configuration the ConfigMap carries, which the pod mounts at the path exim reads."""
    config_map = the_one(monitor, CONFIG_MAP)
    (mount,) = [it for it in container_of(monitor)['volumeMounts'] if it['mountPath'] == CONFIGURATION]
    (volume,) = [it for it in pod_of(monitor)['spec']['volumes'] if it['name'] == mount['name']]
    assert volume['configMap']['name'] == config_map['metadata']['name']
    return config_map['data'][mount['subPath']]


def option(config: str, name: str) -> str:
    """The value of one of exim's options, set once in the configuration."""
    (value,) = re.findall(rf'^\s*{re.escape(name)}\s*=\s*(.*?)\s*$', config, re.MULTILINE)
    return value


def dkim_volume(monitor: Recorder) -> tuple[Path, dict[str, Any]]:
    """The key file the configuration signs with, and the volume the pod mounts its directory from."""
    key = Path(option(configuration(monitor), 'dkim_private_key'))
    mounted = {Path(it['mountPath']): it['name'] for it in container_of(monitor)['volumeMounts']}
    volumes = {it['name']: it for it in pod_of(monitor)['spec']['volumes']}
    return key, volumes[mounted[key.parent]]


# -- the DKIM key ----------------------------------------------------------------


def test_the_relay_signs_with_the_sealed_key_at_the_path_its_configuration_reads(relay: Recorder) -> None:
    """The key is the sealed value's, as its row declares it, and nothing else issues one (declarative/workloads.md §5)."""
    row = conventions.sealed.DKIM_EXIM
    secret = sealed_secret(relay, row.name)
    key, volume = dkim_volume(relay)

    assert secret['metadata']['namespace'] == row.namespace
    assert secret['spec']['encryptedData'] == DKIM
    assert secret['metadata']['annotations'] == {f'sealedsecrets.bitnami.com/{row.scope.value}': 'true'}
    assert volume['secret']['secretName'] == row.name
    assert (key.name,) == row.keys
    assert not [typ for typ in relay.types if typ.endswith(':Certificate')]


def test_the_pods_user_reads_the_dkim_key_and_no_other_user_does(relay: Recorder) -> None:
    """exim defers every message it cannot sign, so the key file is readable by the pod's user, as the kubelet lays it out.

    The kubelet writes a Secret's files owned by root, in the pod's
    `fsGroup` where it has one and root's group otherwise, at the volume's
    `defaultMode`; a process of the pod carries its `runAsGroup`, its
    supplemental groups and its `fsGroup`.
    """
    context = pod_of(relay)['spec']['securityContext']
    _, volume = dkim_volume(relay)
    mode = int(volume['secret'].get('defaultMode', 0o644))
    group = int(context.get('fsGroup', 0))
    groups = {int(context['runAsGroup']), *map(int, context.get('supplementalGroups', []))}
    if 'fsGroup' in context:
        groups.add(group)

    assert int(context['runAsUser']) != 0
    assert group in groups and mode & 0o040, f'mode {mode:o}, group {group}, the pod in {sorted(groups)}'
    assert not mode & 0o004, f'mode {mode:o} lets every user read the key'


def test_the_relay_signs_under_the_selector_every_mail_zone_publishes_in_a_zone_that_publishes_it(
    relay: Recorder,
) -> None:
    """The selector and the signing domain name a record the `dns` stack declares, carrying the key's public half."""
    config = configuration(relay)
    selector = option(config, 'dkim_selector')
    domain = option(config, 'dkim_domain')
    anchors = dns_base.AnchorAddresses(cluster_v4='203.0.113.10', cluster_v6='2001:db8::10', vip1_v4='203.0.113.20')

    assert domain in dns_base.MAIL_ZONES
    for zone in dns_base.MAIL_ZONES:
        published = [
            ''.join(re.findall(r'"([^"]*)"', str(record.content)))
            for record in zone_records(zone, dns_base.blocks(anchors=anchors))
            if record.label == f'{selector}._domainkey'
        ]
        assert published == [conventions.DKIM_K8S], zone


# -- the SMTP credential ---------------------------------------------------------


def stack_sealed_values() -> object:
    """What the `apps` stack's configuration holds under the sealed key, as the program reads it."""
    project = yaml.safe_load((ROOT / 'Pulumi.yaml').read_text())['name']
    config = yaml.safe_load((ROOT / f'Pulumi.{conventions.STACK_NAMES.apps}.yaml').read_text())['config']
    return config[f'{project}:{conventions.sealed.CONFIG_KEY}']


def test_the_ported_credential_is_read_from_the_stack_configuration() -> None:
    """Each data key the relay reads is one the `apps` stack holds the ported ciphertext under."""
    held = cast('dict[str, dict[str, str]]', stack_sealed_values())

    assert sealed_ciphertext(SMTP, held) == {key: held[SMTP.name][key] for key in SMTP.keys}


def test_the_credential_is_sealed_to_the_relays_namespace_and_reaches_exim(relay: Recorder) -> None:
    """The ported Secret is namespace-wide in the relay's namespace, and each of its keys reaches the container."""
    namespace = the_one(relay, NAMESPACE_TYPE)['metadata']['name']
    secret = sealed_secret(relay, SMTP.name)
    refs = [it['valueFrom']['secretKeyRef'] for it in container_of(relay)['env']]

    assert namespace == conventions.sealed.DKIM_EXIM.namespace == secret['metadata']['namespace']
    assert secret['metadata']['annotations'] == {'sealedsecrets.bitnami.com/namespace-wide': 'true'}
    assert secret['spec']['encryptedData'] == SMTP_CIPHERTEXT
    assert {ref['name'] for ref in refs} == {SMTP.name}
    assert sorted(ref['key'] for ref in refs) == sorted(SMTP.keys)


def test_the_account_and_the_password_reach_the_authenticators_in_their_places(relay: Recorder) -> None:
    """PLAIN sends the account first and the password second, each read from the variable its key reaches."""
    env = {it['name']: it['valueFrom']['secretKeyRef']['key'] for it in container_of(relay)['env']}
    (plain,) = re.findall(r'public_name\s*=\s*PLAIN\s*\n\s*client_send\s*=\s*(.*)', configuration(relay))

    assert [env[name] for name in re.findall(r'\$\{env\{(\w+)\}', plain)] == list(SMTP.keys)


# -- the pod ---------------------------------------------------------------------


def holds(expression: Mapping[str, Any], labels: Mapping[str, str]) -> bool:
    """Whether `labels` satisfy one requirement of a Kubernetes label selector."""
    present = expression['key'] in labels
    match expression['operator']:
        case 'Exists':
            return present
        case 'DoesNotExist':
            return not present
        case 'In':
            return present and labels[expression['key']] in expression['values']
        case operator:
            raise AssertionError(f'no reading of {operator!r} here')


def selects(expressions: list[dict[str, Any]], labels: Mapping[str, str]) -> bool:
    return all(holds(it, labels) for it in expressions)


def test_the_relay_runs_on_the_worker_and_on_no_control_plane_node(relay: Recorder) -> None:
    """The worker is the one node of the cluster that is no control plane node (`components.monitoring`)."""
    affinity = pod_of(relay)['spec']['affinity']['nodeAffinity']
    terms = affinity['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms']

    def schedules_to(labels: dict[str, str]) -> bool:
        return any(selects(term['matchExpressions'], labels) for term in terms)

    assert schedules_to({'kubernetes.io/hostname': 'worker'})
    assert not schedules_to({'kubernetes.io/hostname': 'cp-1', CONTROL_PLANE_LABEL: ''})


def test_the_relay_runs_restricted_as_a_numeric_user_on_a_read_only_root(relay: Recorder) -> None:
    """Its namespace enforces `restricted`, and the image's user is a name, so the pod states a number."""
    namespace = the_one(relay, NAMESPACE_TYPE)
    spec = pod_of(relay)['spec']
    context = spec['securityContext']
    container = container_of(relay)

    assert namespace['metadata']['labels']['pod-security.kubernetes.io/enforce'] == 'restricted'
    assert context['runAsNonRoot'] is True
    # The mock hands numbers back as floats, so the user is held to a whole one.
    assert context['runAsUser'] == int(context['runAsUser']) > 0
    assert context['seccompProfile'] == {'type': 'RuntimeDefault'}
    assert container['securityContext']['allowPrivilegeEscalation'] is False
    assert container['securityContext']['capabilities'] == {'drop': ['ALL']}
    assert container['securityContext']['readOnlyRootFilesystem'] is True
    assert container['image'] == str(IMAGE)


def test_the_service_forwards_to_the_port_the_daemon_listens_on(relay: Recorder) -> None:
    """The name the senders are given, the Service, the container's port and the configuration agree."""
    service = the_one(relay, SERVICE)
    pod = pod_of(relay)
    (port,) = service['spec']['ports']
    named = {it['name']: it['containerPort'] for it in container_of(relay)['ports']}

    assert f'{service["metadata"]["name"]}.{service["metadata"]["namespace"]}.svc' == HOST
    assert service['spec']['selector'].items() <= pod['metadata']['labels'].items()
    assert named[port['targetPort']] == port['port'] == int(option(configuration(relay), 'daemon_smtp_ports'))


def test_the_senders_pods_may_reach_the_smtp_port_and_no_other_namespaces_may(relay: Recorder) -> None:
    """The namespace's default-deny admits no other namespace's pods; this admits the senders' alone, on SMTP alone."""
    (policy,) = [it.inputs for it in relay.of_type(POLICY) if it.inputs['metadata']['name'] == 'senders']
    (rule,) = policy['spec']['ingress']
    (ports,) = rule['toPorts']

    def admitted(namespace: str) -> bool:
        pod = {NAMESPACE_LABEL: namespace, 'app': 'a-sender'}
        return any(selects(peer.get('matchExpressions', []), pod) for peer in rule['fromEndpoints'])

    assert policy['spec']['endpointSelector']['matchLabels'].items() <= pod_of(relay)['metadata']['labels'].items()
    assert all(admitted(namespace) for namespace in SENDERS)
    assert not admitted('another-application')
    assert 'fromEntities' not in rule
    assert ports['ports'] == [
        {'port': str(int(the_one(relay, SERVICE)['spec']['ports'][0]['port'])), 'protocol': 'TCP'}
    ]


@pytest.mark.asyncio
async def test_a_relay_handed_no_senders_admits_no_other_namespace() -> None:
    """With no sender, nothing beyond the namespace's default-deny admits a pod."""
    monitor = await declared(())

    assert not [it for it in monitor.of_type(POLICY) if it.inputs['metadata']['name'] == 'senders']


# -- the pinned image's exim -----------------------------------------------------


@dataclass(frozen=True)
class Placed:
    """A file laid into the container before it starts: its bytes and mode, owned by root."""

    content: bytes
    mode: int = 0o644


def as_the_pod(
    monitor: Recorder, script: str, files: Mapping[str, Placed], env: Mapping[str, str] | None = None
) -> str:
    """What `script` writes to its standard output in a container of the pinned image run as the pod runs.

    The files are laid in before the start, owned by root, through a stream
    `podman cp` reads, because the podman a case reaches may run where the
    case's files are not; a root-owned file is also what exim asks of its
    configuration at the path it reads by default.
    """
    require_podman(WHY)
    reference = pulled(pinned('exim'))
    context = pod_of(monitor)['spec']['securityContext']
    name = f'kluster-test-exim-{uuid.uuid4().hex[:12]}'
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        for path, placed in files.items():
            member = tarfile.TarInfo(path.lstrip('/'))
            member.size, member.mode, member.uid, member.gid = len(placed.content), placed.mode, 0, 0
            tar.addfile(member, io.BytesIO(placed.content))
    with process_sessions.removing(name, timeout=COMMAND_TIMEOUT):
        _ = process_sessions.run(
            [
                'podman',
                'create',
                '--rm',
                f'--timeout={CONTAINER_TIMEOUT}',
                f'--label={LABEL}',
                f'--name={name}',
                '--network=none',
                '--read-only',
                '--cap-drop=all',
                '--security-opt=no-new-privileges',
                f'--user={int(context["runAsUser"])}:{int(context["runAsGroup"])}',
                f'--tmpfs={SPOOL}:mode=1777',
                *(f'--env={key}={value}' for key, value in (env or {}).items()),
                reference,
                'sh',
                '-c',
                script,
            ],
            timeout=COMMAND_TIMEOUT,
            check=True,
        )
        if files:
            _ = process_sessions.run(
                ['podman', 'cp', '--archive=false', '-', f'{name}:/'],
                input=archive.getvalue(),
                timeout=COMMAND_TIMEOUT,
                check=True,
            )
        done = process_sessions.run(['podman', 'start', '--attach', name], timeout=COMMAND_TIMEOUT, check=True)
    return done.stdout.decode()


def the_configuration(monitor: Recorder) -> dict[str, Placed]:
    return {CONFIGURATION: Placed(configuration(monitor).encode())}


def replies(out: str) -> list[str]:
    """The SMTP replies in what a case printed, each line that starts with a reply code."""
    return [line for line in out.splitlines() if re.match(r'\d{3}[ -]', line)]


def test_the_pod_runs_as_the_exim_user_of_the_pinned_image(relay: Recorder) -> None:
    """exim starts its daemon for an administrative user alone: `exim`, whose number the image's packages chose."""
    context = pod_of(relay)['spec']['securityContext']

    out = as_the_pod(relay, 'id -u exim && id -g exim && id -u && id -g', {})

    assert out.split() == [str(int(context['runAsUser'])), str(int(context['runAsGroup']))] * 2


def session(monitor: Recorder, client: ipaddress.IPv4Address | ipaddress.IPv6Address) -> list[str]:
    """exim's replies to one SMTP session from `client`, which `-bh` runs against the declared configuration."""
    commands = (
        'EHLO sender.example\r\n'
        f'MAIL FROM:<kluster@{conventions.ZONE_PRIMARY}>\r\n'
        'RCPT TO:<someone@example.com>\r\n'
        'QUIT\r\n'
    )
    out = as_the_pod(
        monitor,
        'printf %s "$SESSION" | exec exim -bh "$CLIENT"',
        the_configuration(monitor),
        env={'SESSION': commands, 'CLIENT': str(client)},
    )
    return replies(out)


def reply_to_rcpt(said: list[str]) -> str:
    """The reply to the RCPT command: the last before the one to QUIT."""
    return said[-2]


def inside() -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """The first and the last address of each pod range."""
    return [
        address
        for network in (conventions.POD_CIDR_V4, conventions.POD_CIDR_V6)
        for address in (network[1], network[-2])
    ]


def outside() -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """The address just below and just above each pod range, and an address at home."""
    return [
        *(
            address
            for network in (conventions.POD_CIDR_V4, conventions.POD_CIDR_V6)
            for address in (network.network_address - 1, network.broadcast_address + 1)
        ),
        ipaddress.ip_address('192.168.1.10'),
    ]


@pytest.mark.parametrize('client', inside(), ids=str)
def test_the_relay_accepts_mail_from_the_pod_ranges(
    relay: Recorder, client: ipaddress.IPv4Address | ipaddress.IPv6Address
) -> None:
    assert reply_to_rcpt(session(relay, client)).startswith('250 ')


@pytest.mark.parametrize('client', outside(), ids=str)
def test_the_relay_refuses_mail_from_outside_the_pod_ranges(
    relay: Recorder, client: ipaddress.IPv4Address | ipaddress.IPv6Address
) -> None:
    assert reply_to_rcpt(session(relay, client)).startswith('550 ')


def test_the_relay_offers_its_senders_no_starttls(relay: Recorder) -> None:
    """It serves no certificate, so a sender that asks for TLS is told there is none to ask for."""
    keywords = [line[4:] for line in session(relay, conventions.POD_CIDR_V4[1]) if line.startswith('250')]

    # The keywords its EHLO reply lists were read: one every exim offers.
    assert 'PIPELINING' in keywords
    assert 'STARTTLS' not in keywords


#: The daemon started as the pod's command starts it, and one session over
#: loopback, each command sent once the reply before it has arrived: the
#: greeting, then EHLO, MAIL, RCPT and QUIT. Loopback is outside the relay
#: range, so the RCPT is refused. The waits are on the replies, bounded by the
#: container's timeout and the command's.
DAEMON_SESSION = rf"""
exim -bdf -q15m & daemon=$!
until nc -z 127.0.0.1 {{port}}; do kill -0 $daemon || exit 3; usleep 1000; done
cd {SPOOL} && mkfifo client
nc 127.0.0.1 {{port}} < client > replies & reader=$!
exec 3> client
replies() {{
    until [ "$(grep -c '^[0-9][0-9][0-9] ' replies)" -ge "$1" ]; do kill -0 $reader || return 0; usleep 1000; done
}}
replies 1; printf 'EHLO sender.example\r\n' >&3
replies 2; printf 'MAIL FROM:<kluster@{conventions.ZONE_PRIMARY}>\r\n' >&3
replies 3; printf 'RCPT TO:<someone@example.com>\r\n' >&3
replies 4; printf 'QUIT\r\n' >&3
replies 5; exec 3>&-; wait $reader
echo '--- replies'; cat replies
"""


def test_the_daemon_refuses_a_recipient_and_goes_on_with_the_session(relay: Recorder) -> None:
    """A refusal is logged where the pod's logs go, and the session continues: the read-only root has no log file to open."""
    port = int(option(configuration(relay), 'daemon_smtp_ports'))

    out = as_the_pod(relay, DAEMON_SESSION.replace('{port}', str(port)), the_configuration(relay))

    log, _, transcript = out.partition('--- replies')
    said = replies(transcript)
    assert said[0].startswith('220 ')
    assert reply_to_rcpt(said).startswith('550 ')
    assert said[-1].startswith('221 ')
    assert not [line for line in said if line.startswith('421')]
    assert 'rejected RCPT <someone@example.com>' in log


#: A smarthost of the test's own that offers no STARTTLS and accepts every
#: command, writing each one it hears to the spool.
LISTENER = rf"""#!/bin/sh
printf '220 listener ESMTP\r\n'
while read -r line; do
    echo "$line" >> {SPOOL}/heard
    case "$line" in
        EHLO*) printf '250-listener\r\n250 AUTH PLAIN LOGIN\r\n';;
        AUTH*) printf '235 accepted\r\n';;
        DATA*) printf '354 go on\r\n'; while read -r body; do [ "$body" = "$(printf '.\r')" ] && break; done
               printf '250 queued\r\n';;
        QUIT*) printf '221 bye\r\n'; exit 0;;
        *) printf '250 OK\r\n';;
    esac
done
"""

#: Where the listener is laid in, and the port it listens on, on loopback.
LISTENER_PATH = '/usr/local/bin/listener'
LISTENER_PORT = 2525


def toward_the_listener(config: str) -> str:
    """The declared configuration with its router pointed at the listener: its transport as declared.

    The router drops a loopback target and refuses one that is the host
    itself, which the listener is, so the copy routes to it and sends there.
    """
    route = option(config, 'route_data')
    ignored = re.compile(r'^\s*ignore_target_hosts\s*=.*\n', re.MULTILINE)
    assert len(ignored.findall(config)) == 1
    pointed = ignored.sub('', config).replace(
        f'route_data = {route}', f'route_data = 127.0.0.1::{LISTENER_PORT}\n  self = send'
    )
    assert pointed.count(f'127.0.0.1::{LISTENER_PORT}') == 1
    return pointed


def test_the_relay_sends_the_credential_to_no_smarthost_that_offers_no_tls(relay: Recorder) -> None:
    """A message for a smarthost that offers no STARTTLS stays queued, and the account and password are never sent."""
    script = f"""
cd {SPOOL}
nc -lk -s 127.0.0.1 -p {LISTENER_PORT} -e {LISTENER_PATH} &
until nc -z 127.0.0.1 {LISTENER_PORT}; do usleep 1000; done
printf 'Subject: test\\n\\nbody\\n' | exim -odq -f kluster@{conventions.ZONE_PRIMARY} someone@example.com
exim -qff
echo "--- queued $(exim -bpc)"
echo '--- heard'; cat heard
"""
    files = {
        CONFIGURATION: Placed(toward_the_listener(configuration(relay)).encode()),
        LISTENER_PATH: Placed(LISTENER.encode(), mode=0o755),
    }
    variables = re.findall(r'\$\{env\{(\w+)\}', configuration(relay))

    out = as_the_pod(relay, script, files, env=dict.fromkeys(variables, 'not-to-be-sent'))

    _, _, rest = out.partition('--- queued ')
    queued, _, heard = rest.partition('--- heard')
    commands = heard.split()
    # The listener was reached: the relay greeted it, so what it did not say it chose not to.
    assert 'EHLO' in commands
    assert 'AUTH' not in commands
    assert queued.strip() == '1'
