"""Properties of the provisioner that are not about talking to OCI.

The one asserted here is a boundary: `provision` is full of `ensure_*`
functions that create what they cannot find, and exactly one lookup must not
do that -- the diagnosis path.
"""

from __future__ import annotations

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
