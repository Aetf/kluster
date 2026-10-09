"""Custom providers: talking to a system Pulumi has no provider for.

One package per system (docs/framework/pulumi.md §5), each holding its
resources, their provider, and whatever transport reaches the system. Today
those are the device files on the gateway, the UniFi controller's rank of a
zone pair's policies and its site BGP configuration, the Talos Image Factory's
artifacts, an AdGuard Home instance's whole configuration, which the `dns`
stack declares, and, for the state-backend appliance, the objects its image is
imported from and its TLS handshake waited for.

A provider is generic code for a *class* of system, so nothing here imports
`kluster.conventions`: which host, which credential and which name are the
caller's decisions, and they arrive as inputs or as stack configuration read
inside the provider's own process.
"""
