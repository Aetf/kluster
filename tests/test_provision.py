"""Properties of the provisioner that are not about talking to OCI.

The one asserted here is a boundary: `provision` is full of `ensure_*`
functions that create what they cannot find, and exactly one lookup must not
do that -- the diagnosis path.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kluster.scripts.state_backend import provision


class _Network:
    def __init__(self, ips: list[Any]) -> None:
        self.ips: list[Any] = ips
        self.created: int = 0

    def list_public_ips(self, **_kwargs: object) -> Any:
        return type('Response', (), {'data': self.ips})()

    def create_public_ip(self, *_args: object, **_kwargs: object) -> Any:  # pragma: no cover
        self.created += 1
        raise AssertionError('a lookup created a reserved address')


class _Client:
    def __init__(self, ips: list[Any]) -> None:
        self.compartment_id: str = 'ocid1.compartment.test'
        self.network: _Network = _Network(ips)


def _ip(name: str, address: str, state: str = 'ASSIGNED') -> Any:
    return type('PublicIp', (), {'display_name': name, 'ip_address': address, 'lifecycle_state': state})()


def test_the_address_is_looked_up_not_reserved() -> None:
    client = _Client([_ip('state-backend-ip', '192.0.2.10')])

    assert provision.reserved_address(client) == '192.0.2.10'  # pyright: ignore[reportArgumentType]
    assert client.network.created == 0


def test_a_missing_address_is_an_error_rather_than_an_allocation() -> None:
    # `ensure_reserved_ip` reserves one when none exists, which is right while
    # provisioning and wrong for `ssh`: a diagnosis command must not allocate
    # cloud resources because it could not find something.
    client = _Client([])

    with pytest.raises(RuntimeError, match='has the appliance been provisioned'):
        _ = provision.reserved_address(client)  # pyright: ignore[reportArgumentType]
    assert client.network.created == 0


def test_a_terminated_address_does_not_count() -> None:
    client = _Client([_ip('state-backend-ip', '192.0.2.10', state='TERMINATED')])

    with pytest.raises(RuntimeError):
        _ = provision.reserved_address(client)  # pyright: ignore[reportArgumentType]


class _Recorder:
    """Enough of the provision surface to watch what a converge run touches."""

    def __init__(self, *, instance_exists: bool) -> None:
        self.instance_exists: bool = instance_exists
        self.minted: int = 0
        self.terminated: int = 0
        self.launched: int = 0


def _returning(value: Any) -> Callable[..., Any]:
    """A typed stand-in: a bare lambda leaves its parameters unannotated."""

    def stub(*_args: object, **_kwargs: object) -> Any:
        return value

    return stub


@pytest.fixture
def converge(monkeypatch: pytest.MonkeyPatch) -> Any:
    from kluster.scripts.credentials import b2
    from kluster.scripts.state_backend import cli, config

    def install(recorder: _Recorder) -> None:
        def mint(*_args: object, **_kwargs: object) -> tuple[str, str]:
            recorder.minted += 1
            return ('key-id', 'key-secret')

        def find(*_args: object, **_kwargs: object) -> Any:
            return type('Instance', (), {'id': 'ocid1.instance.existing'})() if recorder.instance_exists else None

        def terminate(*_args: object, **_kwargs: object) -> None:
            recorder.terminated += 1
            recorder.instance_exists = False

        def launch(*_args: object, **_kwargs: object) -> str:
            recorder.launched += 1
            return 'ocid1.instance.new'

        monkeypatch.setattr(b2.Session, 'from_entry', staticmethod(_returning(object())))
        monkeypatch.setattr(b2, 'ensure_bucket', _returning('bucket-id'))
        monkeypatch.setattr(b2, 'mint_dump_key', mint)
        monkeypatch.setattr(provision.Oci, 'load', classmethod(_returning(object())))
        monkeypatch.setattr(provision, 'ensure_network', _returning(('vcn', 'subnet')))
        monkeypatch.setattr(provision, 'ensure_security_group', _returning('nsg'))
        monkeypatch.setattr(provision, 'ensure_reserved_ip', _returning(('ip-id', '192.0.2.10')))
        monkeypatch.setattr(provision, 'ensure_image', _returning('image'))
        monkeypatch.setattr(provision, 'find_instance', find)
        monkeypatch.setattr(provision, 'terminate_instance', terminate)
        monkeypatch.setattr(provision, 'ensure_instance', launch)
        monkeypatch.setattr(provision, 'attach_reserved_ip', _returning(None))
        monkeypatch.setattr(provision, 'wait_for_backend', _returning(True))
        monkeypatch.setattr(config, 'render_ignition', _returning('ignition'))
        monkeypatch.setattr(config, 'client_bundle', _returning(object()))
        monkeypatch.setattr(config, 'write_client_bundle', _returning(None))
        monkeypatch.setattr(cli.seeds, 'load_seed', _returning(bytes(32)))

    return install


def test_a_converge_run_does_not_revoke_the_running_box_s_dump_key(converge: Any) -> None:
    """B2 returns an application key's secret once.

    So the box's copy cannot be read back, and minting a replacement revokes
    what the box is holding. A run that then leaves the instance alone would
    break the nightly dump silently until it next fires -- which is what it
    did.
    """
    from kluster.scripts.state_backend import cli

    recorder = _Recorder(instance_exists=True)
    converge(recorder)

    assert cli._provision(object(), seed_entry='e', compartment=None, replace=False) == 0  # pyright: ignore[reportPrivateUsage, reportArgumentType]

    assert recorder.minted == 0
    assert recorder.terminated == 0
    assert recorder.launched == 0


def test_replace_terminates_first_then_mints_for_the_new_box(converge: Any) -> None:
    from kluster.scripts.state_backend import cli

    recorder = _Recorder(instance_exists=True)
    converge(recorder)

    assert cli._provision(object(), seed_entry='e', compartment=None, replace=True) == 0  # pyright: ignore[reportPrivateUsage, reportArgumentType]

    assert (recorder.terminated, recorder.minted, recorder.launched) == (1, 1, 1)


def test_a_first_run_mints_and_launches(converge: Any) -> None:
    from kluster.scripts.state_backend import cli

    recorder = _Recorder(instance_exists=False)
    converge(recorder)

    assert cli._provision(object(), seed_entry='e', compartment=None, replace=False) == 0  # pyright: ignore[reportPrivateUsage, reportArgumentType]

    assert (recorder.terminated, recorder.minted, recorder.launched) == (0, 1, 1)
