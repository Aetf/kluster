# Bring-Up

The one pass from the gateway window to a verified, empty cluster: the
order everything runs in, who runs each step, what it waits for, what
says it worked, and what a failure does. The steps themselves are
written where they are owned:
[gateway-cutover.md](gateway-cutover.md),
[gateway.md](gateway.md) §2.5,
[credentials.md](../credentials.md) §4.1,
[cluster/migration.md](../cluster/migration.md) §1 and
[rfc-007](../rfc/rfc-007-k8s-base-and-apps.md) §14. This document repeats
none of their commands. It is what none of them is: the sequence.

**It retires when stage 9 has run.** Everything here runs once. The
procedures that recur are written in the documents above and outlive it,
so the retirement sentence has nothing to move, and nothing but this
file goes. The first wave's verification takes over what §6 moves. Until
then, a step that one of those documents changes is changed here in the
same pull request.

"Empty" means no application of the migration: the cluster holds the
closed component list and the echo server rfc-007 §9.2 uses as its exit
criterion. The legacy estate is untouched throughout. Outside the
window's own minutes, a stage below can stop for as long as it takes,
with nothing degraded for anyone.

## 1. What has to be merged, and why it is little

**The code of `k8s-base` merges ahead of the cluster.** A merge applies
nothing before `deploy.yml` is enabled with the kubeconfig in the stack
files ([sources-of-truth.md](../sources-of-truth.md) §3), which is
stage 4. So rfc-007 slices 9, 10 and 11 are built and merged
ahead of the window, so that the cluster's first `up` declares all of
them at once. A stage's row says what else it needs: stage 7 needs the
migration design's `route`, and stage 8 ends with a small pull request.

**A consumer of a sealed value is declared with its value absent, and
waits.** A sealed value can be made only once the controller runs, and
the controller runs only after the first `up`. So until stage 8 ends, an
absent value declares no SealedSecret and warns at the run, and whatever
reads it stays pending: the issuer's certificates, the BGP session, and
alertmanager's receiver. The value's arrival is a commit to the stack
file, stage 6, and no code changes with it. Stage 8's last step is the
pull request that deletes the tolerance, and from then on an absent
value stops the run by name, as an unusable kubeconfig does. rfc-007
slice 9 builds it, with a test of the absent path, and until it goes it
is a dated exception to style/pulumi.md's rule that absence stops the
run at the read.

**The rest of the pass is operator steps and readings.** Three parties
run it: the **operator**, from the checkout that holds `.credentials/`;
the **chain**, `deploy.yml` on a merge; and the **dispatcher**, which
reads and records. An agent holds no credential here, so no agent takes
a step that needs one.

## 2. The stages

| Stage | What | Who | Needs | Gate | On failure |
| --- | --- | --- | --- | --- | --- |
| 0 | The prerequisites: the controller probe of gateway-cutover.md §3 passing for every zone pair, rfc-007 slices 9 to 11 merged, and Home Assistant's intake automation and check-in timer made (rfc-007 slice 14) | operator, dispatcher | nothing | gateway-cutover.md §3's list; the three slices merged; the automation and the timer present | The window waits. Nothing has changed |
| 1 | **The window**: gateway-cutover.md §3's readings, its §4 steps 0 to 4, and the verification of its §5 up to step 5 | operator | stage 0 | every pass of gateway-cutover.md §5 before step 5 | gateway-cutover.md §6, which undoes it whole |
| 2 | **`physical` in full**, in the order of gateway.md §2.5: gateway-cutover.md §5's step 5 with the overlay's network and members left out, and the disk grown on the host; the gateway's roster entry and the run that creates its member alone; the homelab host refusing the home routes; step 5's run with no targets and the readings after it; `workerGua`; and the knob unset | operator | stage 1, and the tenancy grant gateway.md §2.5 names | a further `up` reports no changes, and a `physical` preview over the overlay comes back clean | Re-run the apply; leave the device alone. gateway-cutover.md §6 stays open throughout, and the run that writes the overlay's routes has its own way back in gateway-cutover.md §5 step 5 |
| 3 | **Readings with no network plugin**: gateway-cutover.md §8.1, the device half of gateway.md §2.4.2, the node volume's size in bytes, and the home uplink's MTU, read off the gateway's WAN interface | operator, dispatcher records | stage 2 | each item's own; at a decimal GB the volume's row grows, and under 1500 `KUBESPAN_MTU` follows the uplink | A failing item is fixed on the empty cluster, and costs only time |
| 4 | **One sitting**: the `dns` first `up` (gateway-cutover.md §8.2), the identity sync and `deploy.yml` enabled (gateway-cutover.md §8.3), and at once `credentials derived sync --only kubeconfig` with both stack files committed | operator | stage 3 | the readings of gateway-cutover.md §8.2 and §8.3, and the copy in both stack files | Each has its way back in gateway-cutover.md §8.2 and §8.3. Disabling `deploy.yml` stops the chain, and undoes no write already made |
| 5 | **The first `k8s-base` and `apps` runs**, which merging stage 4's commit starts | chain, dispatcher reads | stage 4 | `up-k8s-base` green, every node `Ready`, no `kube-proxy` DaemonSet, the sealing certificate answering, and the host's available memory read with the worker running | Fix forward in a pull request; the chain runs it. §3 |
| 6 | **One sealing sitting**: `credentials derived cloudflare-dns01 mint`, `bgp seal` and `alert-webhook record` (credentials.md §4.1 stage 11), the webhook at Home Assistant's own address, so that an in-cluster alert stays direct, one commit to `Pulumi.k8s-base.yaml` | operator, chain on the merge | stage 5, and the webhook of stage 0 | a certificate issued for the primary zone; each Gateway's Service holding exactly its addresses; a route in the gateway's kernel table for a VIP of each family, the IPv6 one through the worker's link-local, which the gateway's neighbor table resolves to the worker's MAC (gateway-cutover.md §5); an alert reaching the phone | Re-run the one command, which writes a fresh ciphertext, and merge it. §3 |
| 7 | **The echo server wired**: rfc-007 slice 12's second half, on `route` as the migration's design composes it | chain | stage 6's certificate, and that design's `route` merged | the exit criterion of rfc-007 §14 slice 12 | Revert it; nothing else stands on it |
| 8 | **The gate**: rfc-007 §14 slice 13's list, and the rest of migration.md §1 item 4. A transcript per item. Then the pull request that deletes the sealed tolerance of §1 | dispatcher, operator for anything a credential reads, a builder for the pull request | stage 7 | every item, with a fallback that flips landing in the document that records it | A failing item is its own issue. No application moves until it closes |
| 9 | **Handing over**: the legacy sealing key carried, the certificate the controller serves confirmed as the cluster's own, and then the backup bucket's writer keys sealed. These open the first wave | operator | stage 8, and the `physical` apply that minted the writer keys | the certificate confirmed, and each key sealed | §3 |

## 3. What a failure costs, by stage

**Through stage 2 the way back is the window's own.** gateway-cutover.md
§6 restores the device, and no step of gateway-cutover.md §4 or §5
closes that way back. What closes it is gateway-cutover.md §7, which
waits for the soak. The overlay's routes to the home are the one write
of stage 2 that §6 does not cover, and gateway-cutover.md §5 step 5
gives them a way back of their own.

**From stage 3 on, nothing serves anyone from the new cluster**, so no
failure is an outage. The cluster can be rebuilt: `physical` holds what
makes it, and `k8s-base` and `apps` hold nothing they cannot declare
again. A stage that fails is fixed forward by a pull request, and the
legacy estate goes on serving.

**Stage 4 is the only stage with a public consequence**, because the
`dns` first `up` writes the live zones. gateway-cutover.md §8.2 states
the way back. The chain's arming is reversed by
disabling `deploy.yml`, which is why that step's reading comes first.

**Stage 6's certificate is read, not re-minted.** A DNS-01 token that
does not work fails at the challenge, before anything reaches the
certificate authority, and the controller goes on retrying by itself:
`kubectl describe challenge` says why.

**Stage 9's key is additive.** A controller keeps every key it holds, so
a carried key that went wrong is deleted, the controller restarted, and
the carry repeated, with nothing already opened lost. The one thing to
read after the import is which certificate the controller now serves
(rfc-007 §6.3).

## 4. What the empty cluster costs meanwhile

| From | What starts | What it costs |
| --- | --- | --- |
| Stage 1 | The device's new layout | Nothing. The gateway serves as it did |
| Stage 2 | The cloud fleet, the balancer, the backup bucket and the worker | The cloud side is free while idle. The worker takes about 1.5 GiB of host memory while idle, and the VPS bills as it always did |
| Stage 5 | The platform on the worker: monitoring first | The worker's real use grows into its 10 GiB, which is why stage 5's gate reads the host's memory |
| Stage 9 | Nothing new | Coexistence is cheap until here. It costs for real from the first wave, which starts stopping and moving applications |

**The pass takes about four days, three of them with the operator**,
and no stage waits on code.

**So the targeted window and the full `up` need not be one day.** Stage 2
follows stage 1 once gateway-cutover.md §5 has passed, and nothing is
lost by a gap:
the device runs as declared, and the way back stays open.

## 5. Beside the sequence

These run alongside and gate no stage:

*   **The DS records**, gateway-cutover.md §8.4, zone by zone, from
    stage 4 on.
*   **The gateway's soak and its cleanup.** The soak ends at stage 2's
    gate. gateway-cutover.md §7 then runs, once its second routing
    reading has taken a reboot.
*   **The resolvers' flip**, row R2 of sources-of-truth.md, after stage 4:
    adguardhome-sync stopped, both instances reading their rewrites
    again, and the declared rule list merged. The echo row is public, so
    no stage here needs it, and the first wave with a row on a home
    Gateway waits on it.
*   **The worker's GPU and growth windows**, which belong to the waves
    and not to this pass.

gateway-cutover.md's update window, which its §8 applies to the steps
that follow the ceremony, holds for stages 2 to 4.

## 6. What moved out of the gate

Three items of the verification lists cannot run on an empty cluster,
and migration.md §1 and physical.md §6 say so: each moves to the first
wave, which is where what it tests arrives.

*   **ExternalAuth failing closed**, migration.md §1 item 4 and
    physical.md §6, needs the authentication gate.
*   **The authentication canary**, physical.md §6, needs the
    authentication service.
*   **The backup bucket's prefix isolation**, physical.md §6, needs a
    writer key with a consumer.

Everything else on those lists runs at stage 8. The check that a raw
LoadBalancer port is answered ahead of the node firewall runs on a
scratch Service on the `syncthing` row, since no raw port exists yet.
It is built with the `port` helper in the echo server's namespace, so
that it runs under the base's default-deny, with the row's own allow.
Its pod runs on the worker, and it answers on TCP and UDP through the
balancer on both families and at the LAN VIP, because that is the path
syncthing's peer port takes: from a cloud node, over KubeSpan, to the
home, and from the home directly. It is deleted when the check has
passed, so that nothing public stays on the port.
