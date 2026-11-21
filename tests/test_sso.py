"""The portal's issuer as its clients reach it (`conventions.sso.Issuer`, `conventions.sso.issuer`).

What the issuer's addresses are is held against the `dns` stack's rewrite of
the same row, in `test_dns_rewrites.py`, and that every client's pod carries
them, in `test_apps_placement.py`. What is held here is what the derivation
refuses.
"""

import pytest

from kluster import conventions
from kluster.conventions.routes import PROXIED, REWRITE, Exposure, Held, Route


@pytest.mark.parametrize(
    'row',
    [Route('auth', internet=PROXIED), Held(Route('auth', internet=PROXIED, media=REWRITE))],
    ids=['internet', 'media'],
)
def test_a_portal_with_no_home_side_on_lan_has_no_issuer(row: Route | Held[Route]) -> None:
    """A client's pod at home would reach it through the uplink alone, as a browser at home would."""
    with pytest.raises(ValueError, match='no home side on `lan`'):
        _ = conventions.sso.issuer(Exposure(routes=(row,)))


def test_an_issuer_names_a_host() -> None:
    with pytest.raises(ValueError, match='names no host'):
        _ = conventions.sso.Issuer('https:///')


def test_an_issuer_at_an_address_takes_no_addresses() -> None:
    """A pod dials an address as it is, so there is no name to answer with another one."""
    with pytest.raises(ValueError, match='is an address'):
        _ = conventions.sso.Issuer('http://127.0.0.1:8080', (conventions.LAN_POOL.default_vip.v4,))


def test_an_issuer_takes_one_address_of_a_family() -> None:
    """A pod's resolver hands back both, and the client dials whichever it orders first."""
    vip, other = conventions.LAN_POOL.default_vip, conventions.LAN_POOL.media_vip
    with pytest.raises(ValueError, match='two addresses of one family'):
        _ = conventions.sso.Issuer('https://auth.example.invalid', (vip.v4, vip.v6, other.v4))
