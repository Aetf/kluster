"""Desired-state files on a UniFi OS device, over SSH.

There is no API for most of what matters on the gateway — routing, the nspawn
services, the script that re-establishes both after a firmware update — but
there is a proven convention: files under `/data`, written idempotently, each
with a hook that runs after it changes. That is what this package drives.

Two modules: `provider` holds the resources and the provider behind them, and
`ssh` the transport they open. There is no registry client here: a pinned
container image is fetched by the device itself, which runs `skopeo` and
`umoci` over the same session, so this package's whole business is the session.
The transport is `asyncssh` rather than a subprocess — each provider operation
runs on a gRPC worker thread and brings up its own event loop, an
asyncio-native client needs no further bridging inside it, it ships its own
type information, and pinning a host key is a parameter to it rather than a
`known_hosts` file assembled on the runner.

The session crosses an untrusted network, so the device's **host key is
pinned**: a first contact that accepted whatever answered would hand an
interposer root on the router.
"""
