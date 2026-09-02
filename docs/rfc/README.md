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

**The process itself is [rfc-004](rfc-004-rfc-process.md)** until its first
slice moves the mechanism to `docs/framework/rfc.md`, where mechanisms
belong: when an RFC is required and when an ops issue is enough, the
sections one carries, the states it moves through and the labels that
carry them, the operator's review gate, how it is amended after
acceptance, and how it is numbered and named. Anyone about to write one
reads that first; anyone about to review one holds it to that document's
§4.

## The index

Every RFC, whatever its state, with the date in its own status header. A
proposal that is still a pull request appears without a link, because its file
is not on `main` yet.

| RFC | Subject | Status | Where its content lives now |
| --- | --- | --- | --- |
| [rfc-001](rfc-001-native-async-inputs.md) | Native async inputs for Pulumi Python components: `async_output`, `resolve`, preview safety. | Implemented | The mechanism is [framework/pulumi.md](../framework/pulumi.md) §1 and §2; the implementation is `src/putils/`. |
| [rfc-002](rfc-002-src-layout-and-the-gateway.md) | The source layout, the gateway as a component tree, custom providers, `conventions`, and the `physical` stack's configuration surface. | Implemented | Its own status header names them. |
| rfc-003 | The `dns` and `github` stacks under the style rules: census homes, the AdGuard rewrite provider, the forge's declarations. | Proposed, in [Aetf/kluster#137](https://github.com/Aetf/kluster/pull/137) | Its §17 names where it will land. |
| [rfc-004](rfc-004-rfc-process.md) | The RFC process: when one is required, what it contains, its lifecycle and review gate, amendment, numbering. | Accepted | Its §11 names where it lands; the mechanism moves to `docs/framework/rfc.md` in the first slice. |
