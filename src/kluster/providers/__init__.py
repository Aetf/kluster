"""Custom providers: talking to a system Pulumi has no provider for.

One package per system (`docs/rfc/rfc-002-src-layout-and-the-gateway.md`
§7): the device files on the gateway, the Talos Image Factory's artefacts, and
the AdGuard rewrites the `dns` stack writes. Each holds its resources, their
provider, and whatever transport reaches the system.

A provider is generic code for a *class* of system, so nothing here imports
`kluster.conventions`: which host, which credential and which name are the
caller's decisions, and they arrive as inputs or as stack configuration read
inside the provider's own process.

Importing this package installs `serialization`'s shim, which is why that
module is imported here and by nothing else: it repairs a leak Pulumi's own
provider serialization would otherwise leave on the pickler, and it has to be
in place before the first dynamic resource is constructed rather than
remembered by each provider.
"""

from kluster.providers.serialization import install_pickler_restore

install_pickler_restore()
