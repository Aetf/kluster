"""The state-backend appliance, as code a script and a stack program can both run.

The appliance is the Postgres box holding every stack's Pulumi state
(docs/physical/state-backend.md). The `state-backend` console script
(`kluster.scripts.state_backend`) drives it; what the script runs that needs
neither the escrow nor the kit lives here, with the files it reads beside it
(docs/style/pulumi.md, "Layering"):

-   `settings`: every pin the appliance has.
-   `machine/`: the machine itself -- the Butane template, the dump script it
    installs, the operator keys and the drill recipient.
-   `render`: the machine as values, rendered into Ignition, and digested for
    comparing a running box against the commit. It takes every key and
    recipient as an argument: minting and recovering them is the caller's.
-   `state`: taking the Pulumi state out of the box, and putting it back.
-   `readiness`: waiting for a box to answer.

Nothing here imports a script, which is what lets the package render without
a checkout around it.
"""
