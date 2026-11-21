"""The portal a run of every application declares, and what a client of it is handed beside it.

`test_apps_placement.py` declares every application component in one run,
each from `declared()` in its application's helper module, and holds a pod
that serves a client of the portal to the issuer's addresses. That relation
reads the run only when the portal and its clients agree: the portal's
`declared()` declares it at `PORTAL`, and each client's hands its
component `ISSUER`, the issuer the program derives from that row, and the
census's client of its name, `client`, whose host its route serves.
"""

from kluster import conventions
from kluster.conventions.routes import PROXIED, REWRITE, Exposure, Route
from kluster.conventions.sso import Client

#: The portal's row as its cut leaves it, on `internet` and at home: cut, so
#: the issuer it derives has the `lan` VIP's addresses.
PORTAL = Exposure(routes=(Route('auth', internet=PROXIED, lan=REWRITE),))

#: The issuer the program derives from `PORTAL` for each client.
ISSUER = conventions.sso.issuer(PORTAL)


def client(name: str) -> Client:
    """The census's client of that name, which the program hands its application."""
    (found,) = [it for it in conventions.sso.CLIENTS if it.name == name]
    return found
