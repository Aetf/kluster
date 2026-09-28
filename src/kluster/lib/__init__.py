"""Shared code, in the layer between the custom providers and `conventions`.

What the layer is for is under "Layering" in docs/style/pulumi.md. The helpers
no area owns: configuration reading, the rendered-configuration mechanism, the
workstation slot mechanics, the acquisition chain a value the operator hands a
run is found through (`acquisition`), the Kubernetes helpers, the version pins,
the `pulumi` CLI runner and the backend and passphrase a run against each stack
is given (`stack_environment`), and what code outside the `credentials` package
needs of the `age` tool and of a state-backend client bundle's layout. And, in
`kluster.lib.<area>`, the code an area's component and a script both run:
`state_backend`, which the `state-backend` script runs today and the
appliance's component will run as well. `putils` is the other home for
shared code — the Pulumi framework, which knows nothing about this
installation.

Nothing here is a component, and nothing here imports a component, a provider,
a stack program or a script. Two of the Kubernetes helpers do declare a resource —
`k8s.helm_chart` and `k8s.sealed_secret` each build one, under the options the
caller passes, and return it — and nothing else here declares any.
"""
