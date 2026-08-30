# RFCs

A design proposal, written before the change it proposes and approved
before any of it is built. Major structural work goes this way round
([framework/dispatch.md](../framework/dispatch.md) §3.1): the
milestone opens with an RFC, the
implementation issues are cut from the accepted text, and the milestone
closes with a review checkpoint against it.

**An RFC is history, not reference.** Once it is built, what the system
*is* belongs to the design documents — `docs/cluster/`, `docs/physical/`,
`docs/declarative/` — and the mechanisms belong to `docs/framework/`; the
RFC keeps the argument, the alternatives weighed, and the measurements
behind them. So an RFC names the documents its content must land in, and
a reader who wants to know how something works today reads those rather
than this directory.

| RFC | Subject | Where its content lives now |
| --- | --- | --- |
| [rfc-001](rfc-001-native-async-inputs.md) | Native async inputs for Pulumi Python components: `async_output`, `resolve`, preview safety. | The mechanism is [framework/pulumi.md](../framework/pulumi.md) §1 and §2; the implementation is `src/putils/`. |
| [rfc-002](rfc-002-src-layout-and-the-gateway.md) | The source layout, the gateway as a component tree, custom providers, `conventions`, and the `physical` stack's configuration surface. | Its own status header names them. |
