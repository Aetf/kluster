"""The monitoring stack's definitions a script can read as well as the component, kept out of the component for that.

`kluster.components.monitoring` declares the monitoring stack, and a script
imports no component (docs/style/pulumi.md, "Layering"), so what the two of
them read lives here:

-   `readings`: what the control planes are measured by, each reading defined
    once, which the component's recording rules, SLO alerts and dashboard are
    made from.
-   `jobs`: the `job` label each scrape gives its series, which the readings
    and the rules select a series's source by.
-   `vmsingle`: how the operator names a vmsingle's Deployment, Service and
    claim, and the port it serves one at, which the component's stores and a
    reader outside the program address a store by.

The names this repository chose, which the component declares under and a
reader outside the program reaches them by, are conventions
(`kluster.conventions.cluster`): the history store's name, etcd's metrics
port and the platform's namespaces. Nothing here declares a resource.
"""
