"""Custom providers: talking to a system Pulumi has no provider for.

One package per system (docs/framework/pulumi.md §5), each holding its
resources, their provider, and whatever transport reaches the system. Today
those are the device files on the gateway, the Talos Image Factory's
artifacts, the AdGuard rewrites the `dns` stack writes, and, for the
state-backend appliance, the objects its image is imported from and its TLS
handshake waited for.

A provider is generic code for a *class* of system, so nothing here imports
`kluster.conventions`: which host, which credential and which name are the
caller's decisions, and they arrive as inputs or as stack configuration read
inside the provider's own process.
"""
