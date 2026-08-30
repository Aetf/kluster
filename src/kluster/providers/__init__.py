"""Custom providers: talking to a system Pulumi has no provider for.

One package per system (`docs/rfc/rfc-002-src-layout-and-the-gateway.md`
§7): the device files on the gateway, the Talos Image Factory's artefacts, and
the AdGuard rewrites the `dns` stack writes. Each holds its resources, their
provider, and whatever transport reaches the system.

A provider is generic code for a *class* of system, so nothing here imports
`kluster.conventions`: which host, which credential and which name are the
caller's decisions, and they arrive as inputs or as stack configuration read
inside the provider's own process.
"""
