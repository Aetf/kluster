"""Helpers with no resources of their own, and no area to belong to.

The installation-generic half of the two helper homes
(`docs/rfc/rfc-002-src-layout-and-the-gateway.md` §2.1): configuration
reading, the workstation slot mechanics, the Kubernetes helpers, the version
pins. `putils` is the other one — the Pulumi framework of rfc-001, which knows
nothing about this installation.

Nothing here declares a resource, and nothing here imports a component, a
provider or a stack program.
"""
