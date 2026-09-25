"""Helpers no area owns, in the layer between the custom providers and `conventions`.

What the layer is for is under "Layering" in docs/style/pulumi.md:
configuration reading, the rendered-configuration mechanism, the workstation
slot mechanics, the Kubernetes helpers, the version pins. `putils` is the other
home for shared code — the Pulumi framework, which knows nothing about this
installation.

Nothing here is a component, and nothing here imports a component, a provider
or a stack program. Two of the Kubernetes helpers do declare a resource —
`k8s.helm_chart` and `k8s.sealed_secret` each build one, under the options the
caller passes, and return it — and nothing else here declares any.
"""
