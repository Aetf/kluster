"""The controller session's own refusals, apart from any provider that opens it.

What each provider reaches through the session, and how the session carries
its key and posture there, is that provider's suite; this one holds what the
session refuses by itself: a configuration without the key, and an answer
that is not a 2xx, which carries no more of the controller's body than the
provider allows.
"""

from __future__ import annotations

import pulumi.dynamic as dynamic
import pytest
import requests

from kluster.providers import unifi_session as session

PROJECT = 'kluster'
URL = 'https://gateway.invalid/proxy/network/v2/api/site/default/bgp/config'
BODY = 'neighbor 192.0.2.10 password a-session-password'


class Provider(session.ControllerProvider):
    """The session with nothing of a provider's own on it."""

    def _version(self) -> str:
        return 'test'


def answer(status: int, body: str = BODY) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = body.encode()  # pyright: ignore[reportPrivateUsage]
    return response


def test_a_configuration_without_the_key_refuses_by_naming_it() -> None:
    with pytest.raises(ValueError, match=session.API_KEY_CONFIG):
        Provider().configure(dynamic.ConfigureRequest(config=dynamic.Config({}, PROJECT)))


@pytest.mark.parametrize('status', [200, 204])
def test_a_success_is_handed_back(status: int) -> None:
    response = answer(status)

    assert session.answered(response, f'GET {URL}') is response


@pytest.mark.parametrize('status', [301, 302, 400, 401, 404, 500])
def test_anything_but_a_2xx_is_refused_with_the_request_and_the_status(status: int) -> None:
    with pytest.raises(session.ControllerRefused, match=f'^GET {URL} answered {status}: '):
        _ = session.answered(answer(status), f'GET {URL}')


def test_a_refusal_carries_the_answer_only_where_the_provider_relays_it() -> None:
    with pytest.raises(session.ControllerRefused) as withheld:
        _ = session.answered(answer(400), f'POST {URL}')
    with pytest.raises(session.ControllerRefused) as relayed:
        _ = session.answered(answer(400), f'POST {URL}', reason=session.relayed)

    assert BODY not in str(withheld.value)
    assert f'{len(BODY)} characters' in str(withheld.value)
    assert BODY in str(relayed.value)


def test_a_refusal_is_the_kind_the_provider_names() -> None:
    class Refused(session.ControllerRefused):
        pass

    with pytest.raises(Refused, match='401'):
        _ = session.answered(answer(401), f'GET {URL}', refused=Refused)
