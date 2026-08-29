"""Helpers with no resources of their own, and no area to belong to.

The estate-generic half of the two helper homes
(`docs/framework/rfc-002-src-layout-and-the-gateway.md` §2.1): the workstation
slot mechanics, the Kubernetes helpers, the version pins. `putils` is the other
one — the Pulumi framework of rfc-001, which knows nothing about this estate.

Nothing here declares a resource, and nothing here imports a component, a
provider or a stack program.
"""
