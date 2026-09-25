"""The gateway's proxy as a suite declares it, and a Caddyfile read back as the vhosts it serves.

The pins are placeholders: nothing here pulls an image, so a digest of the
right shape is all a declaration needs. `served` reads a Caddyfile, the
render's or the device's, as what each vhost tells Caddy, so two files compare
on content rather than layout.
"""

from __future__ import annotations

import re

from kluster import conventions
from kluster.components.gateway import container

ACME_TOKEN = 'a-zone-scoped-token'

DIGEST = f'sha256:{"f" * 64}'
TAG = '7'


def pin(service: str) -> container.Rootfs:
    return container.Rootfs(repository=f'registry.invalid/installation/{service}', tag=TAG, digest=DIGEST)


def caddy(
    legacy: tuple[conventions.gateway.LegacyVhost, ...] = conventions.gateway.LEGACY_VHOSTS,
) -> container.CaddyService:
    """The proxy as the stack declares it, with the census a case is about."""
    return container.CaddyService(
        service=conventions.gateway.CADDY,
        pin=pin('caddy'),
        acme_token=ACME_TOKEN,
        vhosts=conventions.gateway.RESOLVERS,
        legacy=legacy,
    )


#: One `@name host <host>` matcher and the `handle` block it guards, which is
#: how both the render and the device's live file spell a vhost. The body ends
#: at the first closing brace back at the block's own indentation.
VHOST_BLOCK = re.compile(
    r'^\t@(?P<matcher>\S+) host (?P<host>\S+)\n\thandle @(?P=matcher) \{\n(?P<body>.*?)\n\t\}$',
    re.MULTILINE | re.DOTALL,
)


def served(caddyfile: str) -> dict[str, tuple[str, ...]]:
    """Each vhost in a Caddyfile, as what its block tells Caddy.

    Keyed by the name clients ask for, so two files are compared on the
    thing they have in common rather than on how they are laid out. What a
    block says is its directives with the spelling taken out: comments and
    indentation dropped, and the two defaults the live file leans on written
    the way the render writes them — an upstream with no scheme is plain HTTP
    on port 80, and `tls` inside a transport is what the `https://` scheme
    already turned on.
    """
    return {match['host']: directives(match['body']) for match in VHOST_BLOCK.finditer(caddyfile)}


def directives(body: str) -> tuple[str, ...]:
    lines = (' '.join(line.split()) for line in body.splitlines())
    return tuple(explicit(line) for line in lines if line and not line.startswith('#') and line != 'tls')


def explicit(directive: str) -> str:
    """One directive with the upstream's scheme and port spelled out."""
    proxy, _, upstream = directive.partition('reverse_proxy ')
    if proxy or not upstream:
        return directive
    dial, brace, trailer = upstream.partition(' {')
    if '://' not in dial:
        dial = f'http://{dial}' if ':' in dial else f'http://{dial}:80'
    return f'reverse_proxy {dial}{brace}{trailer}'
