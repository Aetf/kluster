# Sources of Truth

Per domain that a stack of this repository owns or will own: the system
whose writes the live state follows today, what the stack has done to
that state, which legacy tracker takes every change until the cutover
and what "take" means there, the step that flips the domain onto the
stack, and where the tracker, the live state and this repository
disagree. Bring-up reads this before its first `up`.

It sits at the docs root beside [operations.md](operations.md) and
[credentials.md](credentials.md) because it crosses every stack and
every legacy tracker, and it lives exactly as long as the transition
does. Neither operations.md, which is the steady state, nor
[cluster/migration.md](cluster/migration.md), which is sequencing and
data movement for the legacy cluster's workloads, could hold it without
a section that turns false when the transition ends. Each flip's pull
request edits its row, and the pull request of the last flip deletes
this file.

## 1. The rules

1.  **The interim rule.** A change to a domain whose live source of
    truth is a legacy tracker lands in that tracker first and in this
    repository second, in the same sitting, and this repository's
    change is never applied until the domain's row flips. What keeps
    each unapplied stack unapplied in the meantime is §3.
2.  **Column 2 is a dated reading, never a status.** A cell holds the
    reading, its date and the command of §4 that took it. The command
    is the answer: a reading older than the row's last change is taken
    again before anything relies on it.
3.  **A flip's pull request edits its row**, in the change that
    performs or records the flip.
4.  **A flip keeps the live service's feature parity**, unless an
    arrangement recorded in its row says otherwise. Whatever the live
    source of truth provides when the row flips is declared here before
    the flip, or named in the row with the arrangement that retires it.

## 2. The table

"Here" is this repository, and "unapplied" is merged and never `up`'d.
B1, C1 and the like are the commands of §4, and every reading below was
taken on 2026-10-04 unless its cell gives another date. N1 to N4 are the notes that follow the table.
Column 5 names issues in the ops repository, where the drift is tracked.

| Row | Domain | (1) Live source of truth today | (2) Stack · apply state | (3) Tracker until the flip · what "take" means | (4) Flip → retirement | (5) Known drift |
|---|---|---|---|---|---|---|
| R1 | `dns` · the Cloudflare zones: base records, mail with DKIM, the `*.zt` block, `legacy.py`'s application records, anchors, CAA, DNSSEC | the `dns` stack, applied as column 3 gives it. Aetf/dns is archived and declares nothing: its pointer commit `fe7cc03` removed `dnsconfig.js` and every workflow, and it holds no secret (G5; note N1) | `dns` · **applied locally.** Its first `up` ran on 2026-11-15 from the operator's checkout (physical/gateway-cutover.md §8.2) and reported 57 created, 67 updated and 55 deleted, the deletes being the records note N1 lists, the set B6 printed before it. A second `up`, from `main` once Aetf/kluster#708 declared a TXT over 255 characters and an SRV's priority as Cloudflare reads them back, followed it. On 2026-11-15 the stack against the zones, the first reading of physical/gateway-cutover.md §8.2, reports nothing but the `status` of the `ZoneDnssec` in each zone it signed first, peifeng.phd, ucw.phd, jiahui.id and jiahui.love, read back as `pending` against the declared `active` | none: flipped. A change is a pull request to `components/dns/` or the conventions it reads, and the merge applies it (§3) | flipped by `kluster-ops#75`'s `dns` first `up` (physical/gateway-cutover.md §8.2) → `kluster-ops#7`: Aetf/dns's pointer commit and its archive, and the revocation at Cloudflare of the token its workflow held (note N1) | the `pending` DNSSEC status of column 2, which physical/gateway-cutover.md §8.4 clears zone by zone (`kluster-ops#75`) |
| R2 | `dns` · the split-horizon answers on alice and bob | alice, by hand. adguardhome-sync, a homelab user unit that is running (H2), copies alice to bob. Both instances answer from `$dnsrewrite` user rules, 18 each, with the rewrite list the stack writes empty and switched off (U3) | `dns` · declares none: B3 lists no rewrite among the stack's types. Its first rows are Aetf/kluster#454, open and held | alice's UI, which the sync carries to bob, and the weekly backup pull, `gateway-backup-pull`, records in yadm's `~/.config/gw-backups` (physical/gateway.md §1.6). Here: every rule the instances answer from, declared in the `user_rules` list of each instance, a list one resource per instance owns whole (`kluster-ops#507`, way (b)), same sitting, unapplied. The `lan.ucw.phd` rules are declared too until their application migrates (rule 4), since the first apply of that list removes every rule it does not declare | Aetf/kluster#454, which declares the user-rules list, merged and applied after R1's flip; adguardhome-sync stopped before it (note N2) → its unit's removal (`kluster-ops#79`); the pull's AdGuard transfer, which keeps column 3's record until the flip (rule 4), is removed in the change that applies #454 | `kluster-ops#507`; `kluster-ops#482`; `kluster-ops#223`; `kluster-ops#143` |
| R3 | `physical` · the cloud fleet in compartment `kluster-physical` (network, nodes, load balancer, addresses, volumes, guardrails) and the Talos day-1 chain | the `physical` stack, for what its runs have created; nothing else declares the rest | `physical` · **created by step 5's first run** (physical/gateway-cutover.md §5), from 2026-11-14, as far as that run has reached: a resource it has not reached is absent, with nothing standing in for it. On 2026-11-14 it stopped short of the instances and the Talos configuration, on the image (`kluster-ops#729`). The console read of OCI (O1) was not taken | none | `physical`'s step 5 in `kluster-ops#75`: its first run, which leaves out the overlay's network and members (physical/gateway-cutover.md §5) → none | none known |
| R4 | `physical` · the homelab's libvirt objects: the pool on the `nodatacow` subvolume, and the worker's volume, seed and domain | the `physical` stack, for what its runs have created. The host preparation under them is aconfmgr's, and applied (physical/homelab-host.md, status) | as R3. On 2026-11-14 the run created the pool and the worker's volume and stopped short of the worker's domain; beside them the host holds the domain `haos` and the pool `images` (H3) | none for these. The host preparation stays aconfmgr's for good, and a pool defined there would fail the first apply (physical/homelab-host.md §4) | the same run as R3, which creates the worker's disk at its declared size; growth to the end state is the host-side procedure of physical/homelab-host.md §1 → none | none known |
| R5 | `physical` · the gateway's device state: boot chain, units, `nspawn` settings and machines, `caddy`, AdGuard's initial state, authorized keys, device secrets, root filesystems. The ZeroTier machine is new | gw-config, yadm's `~/.config/gw-config`, pushed by hand with `deploy.sh`; root filesystems pushed by homelab-containers. Its dry run passes against the device (U1), which runs `adguard-alice`, `adguard-bob` and `caddy` under an active `udm-boot` (U2) | as R3 | gw-config: edit, `./deploy.sh`, the restart it prints, `yadm commit`. Here: the same change in `components/gateway/` or `conventions.gateway`, same sitting, unapplied | the window of physical/gateway-cutover.md, after which gw-config is frozen (note N3) → physical/gateway-cutover.md §7 after the soak; `kluster-ops#79` | `kluster-ops#508`: the live `Caddyfile` carries the Bilibili steering (U4), which nothing here declares |
| R6 | `physical` · the controller census of declarative/physical.md §4: the cluster VLAN's network, its zone, every custom policy on a pair the census orders, whatever traffic it is about, and their order, the v6 pinhole, the v4 peer-port forward, static host entries, the site's BGP configuration | the `physical` stack: the window's targeted run applied the cluster VLAN's network, its zone, the address groups and the policies and orders of the census's pairs on 2026-11-05 (`kluster-ops#75`), and the retirement's targeted run applied the site's BGP configuration on 2026-11-14 (physical/gateway-cutover.md §7, `kluster-ops#736`). The pinhole names the worker's GUA as each run derives it from the delegated prefix it reads; the forward waits for Wave D, and the peer port's WAN side stays the legacy qbittorrent's until then (physical/gateway.md §4.2) | `physical` · **applied by the window's targeted run**, 2026-11-05 (`kluster-ops#75`), which read the census's policies ranked ahead of the predefined ones on Internal → External; the site's BGP configuration **applied by the retirement's targeted run**, 2026-11-14 (`kluster-ops#736`), after which the controller held one configuration, switched on. The console read (U5) was not taken | none: a custom policy on a pair the census orders is a census row, and one found in the console is drift (physical/gateway.md §4.2). What the census leaves to the console, the pairs it does not order, stays there for good, recovered from the UniFi autobackup (physical/gateway-cutover.md §7) | flipped by the window's targeted run; the pinhole by the first apply of the whole stack; the forward in Wave D (`qbittorrentOnWorker`, migration.md §2) → none | none known |
| R7 | `physical` · ZeroTier Central: routes, flow rules, managed DNS, members | Central, hand-kept in its console (physical/gateway.md §2.1). It holds the route `10.144.0.0/16`, default rules and no managed DNS (Z2). Its RFC 4193 and 6PLANE addressing is on: the homelab member's overlay interface carries one address of each, derived from the network id (H4, 2026-10-24), and Z2's `v6` reads the setting itself, not yet taken. Its members match `conventions.overlay.ROSTER`, which also lists `ci-dns` and `ci-physical`, not yet created, and `udm`, whose member the ceremony's step 2 creates (Z3, Z4). Beside them, every kluster-code preview and deploy job authorizes a member of its own at Central with a Central token, outside the roster, and deletes it when the job ends, so Z3 lists one only while a job runs. Those members draw their addresses from the IPv4 assignment pools, which Z2 reads and no reading here records | as R3; the network enters state in the window's targeted run. The managed DNS of `kluster-ops#69`'s first slice is merged and not live (Z2) | none. "Take" is the Central change plus the same change to `conventions.overlay` (`ROSTER`, `MANAGED_ROUTES`), same sitting: the converge deletes a route the census lacks, and admits no member the roster lacks (physical/gateway.md §2.1–2.3) | the window's targeted run adopts the network at Central's values. Once the gateway's entry has landed, a run targeting its member alone creates it and the device is read answering at its overlay address; and once that run and the rest of the stack's first run have passed, the run with no targets converges the network, its default route via the gateway included (physical/gateway.md §2.2, §2.5). The converge turns RFC 4193 and 6PLANE off, so every member loses its two derived IPv6 addresses: the single-family overlay of physical/gateway.md §2.2 is the arrangement (rule 4). The converge also replaces Central's assignment pools with every overlay host address but the roster's and the gateway's (physical/gateway.md §2.1), and admits and removes none of kluster-code's self-authorized CI members: they keep joining outside the roster, the arrangement retired by the step of `kluster-ops#79` that disables kluster-code's workflows or archives the repository and deletes its `ZEROTIER_CENTRAL_TOKEN` at Central (rule 4; threat-model.md §2.10) → hand edits stop | none known |
| R8 | `physical` · the B2 bucket `aetf-kluster-backup` and its keys | the `physical` stack | as R3: the bucket and its keys are among what step 5's first run created on 2026-11-14. The console read of B2 (O2) was not taken | none | the same run as R3, after `credentials derived b2-management mint` (credentials.md) → none | none known |
| R9 | `state-backend` · the appliance: compartment `kluster` (network, subnet, reserved address, image, image bucket, instance), the B2 dump bucket and dump key | the `state-backend` stack (physical/state-backend.md, status). Its box, `state-backend-vm`, created on 2026-10-09T08:50:57Z, replaced the one the appliance's earlier script built in the live drill of rfc-006 §14 slice 6, and what that script had made beside it was deleted by hand (`kluster-ops#462`, steps 0–8). Aetf/kluster#414 removed the write path from that script | `state-backend` · **applied locally**, its checkpoint committed (framework/pulumi.md §3.3): the one on `main` holds 18 resources, written 2026-10-09T13:23:56-07:00 by CLI v3.267.0, after the drill's last `up` (B4, read on 2026-10-09). No backend holds it, by design | none: flipped | flipped by that drill → slice 7 removes the adoption | none known. `kluster-ops#484`, on the dump key's id, is ruled (a) |
| R10 | `github` · repositories, Environments, gates, protection, labels, variables | the `github` stack. Apps, installations, secret values and account roots are console state by design (framework/github.md §4) | `github` · **applied locally**, as framework/github.md §1 has it. 18 resources (B1). The last `update` succeeded on 2026-10-01; earlier ones ran on 2026-09-25 and 2026-09-18, after a `resource-import` on 2026-09-05, and on 2026-09-25 and 2026-10-01 a failed update came minutes before the one that succeeded (B2). Its plan finds every resource the same (G1) | none: flipped | flipped → none | none known. No schedule runs G1 (framework/github.md §1) |
| R11 | `k8s-base` · everything cluster-scoped in the new cluster | the `k8s-base` stack, for the platform declarative/cluster-infra.md §1 lists. The legacy counterpart is kluster-code's `BaseCluster`, in the legacy cluster, which the ambient `kubectl` context reaches (K3) | `k8s-base` · **applied by the deploy chain**: its first `up` is `up-k8s-base` on the merge that committed the copy of the cluster credential into its configuration (`kluster-ops#767`, operations.md §2.5), and every merge applies it since (§3). The reading after it (B1, B2) was not taken | none: flipped. What crosses from the legacy cluster is its sealing key, imported by hand at the first ported manifest (rfc-007 §6.3) | flipped by that first `up` (`kluster-ops#767`) → the legacy base retires with kluster-code (`kluster-ops#79`) | none known |
| R12 | `apps` · each application: namespace, workload, storage, routes, public records | per application, the legacy cluster, written by kluster-code's `dev` stack, where the merge applies. Its last `update` succeeded on 2026-10-04 from CI, at the head of kluster-code's `main` (K1, K2), and its deploy workflow is succeeding (G6). The applications' public records are R1's | `apps` · **applied by the deploy chain**, with nothing beyond its providers: its first `up` is `up-apps` on the merge that committed the copy of the cluster credential into its configuration (`kluster-ops#767`, operations.md §2.5), and every merge applies it since (§3). The reading after it (B1, B2) was not taken | kluster-code: edit, merge. Here: nothing until the application migrates, except its records, which follow R1 and are declared in `legacy.py` | per application, its wave's land and cut (migration.md §0 rule 1, §2): the land wires its component with its public rows held, and the cut deletes its `legacy.py` block and releases the rows (declarative/dns.md §6, note N4) → its kluster-code component removed after verification; `kluster-ops#79` | none known |
| R13 | `apps` · the host services that move into the cluster: qbittorrent-nox and seedwatch (Wave D); thread-dashboard stays a service of the host (migration.md §2) | the homelab host: `qbittorrent-nox@aetf` (aconfmgr), and the `seedwatch` user unit (yadm), both running (H2) | `apps` · declares none of them | aconfmgr or yadm: edit, then `sudo aconfmgr apply`, or `systemctl --user daemon-reload` and a restart, then commit | the wave's land and cut (migration.md §2) → the removal commits in yadm and aconfmgr (migration.md §0 rule 3, §4); `kluster-ops#79` | none known |

**No stack owns, now or later:** the homelab host itself under yadm and
aconfmgr, with the HAOS domain, dmarc-check and the NAS role
(migration.md §2, "not migrating"; declarative/physical.md §3); the
legacy cluster's state backend, the `kluster-state-backend` user unit,
which is running (H2) and whose Postgres stops last, in Wave F
(migration.md §4); and the controller's and the forge's console state
outside their censuses, the controller's holding whole every pair it
orders, so nothing custom on such a pair is console state.

**N1. R1's flip.** The `dns` first `up` (`kluster-ops#75`), run from
the operator's checkout on 2026-11-15 as physical/gateway-cutover.md
§8.2 gives it, is the zones' cutover: from it on, the `dns` stack is
what writes every zone. Aetf/dns is retired. Its pointer commit,
`fe7cc03`, removed `dnsconfig.js`, `creds.json`, `spfcache.json` and
every workflow and left a README pointing here; its
`CLOUDFLARE_API_TOKEN` secret is deleted, and the repository is
archived (G5). The token that secret held is revoked at Cloudflare, so the way back needs a token minted for it (`kluster-ops#7`).

**DNSControl's last declaration is the emergency way back**:
`dnsconfig.js` at `47ab103`, the parent of `fe7cc03`. Reading C1
previews what it would change in the live zones, and
physical/gateway-cutover.md §8.2, under "The way back is forward",
gives how it is applied, which starts with lifting Aetf/dns's archive,
and what it costs.

**The first `up` deleted the `DnsRecord`s in the `dns` state that the
program does not declare.** The import took the live records, so the
state held what DNSControl published, and what of that the declaration
leaves out the first `up` deleted at Cloudflare, but for the last class
below, which Cloudflare no longer held. Each falls in one of these
classes, and each class has its ruling. The members are C1's live set
of 2026-10-04, Aetf/dns at `e865032`, less the declaration, listed by
zone and label:

-   **Every record of a parked zone but its apex and `www`**, the copies
    DNSControl published there: rfc-003 §5.3 and §5.4. In each of
    `peifeng.phd` and `ucw.phd`:
    -   the legacy anchor `archvps.hosts`;
    -   the `*.zt` names of DNSControl's block, which are not the
        roster's: `Aetf-Arch-XPS.zt`, `OnePlus6T.zt`, `Aetf-Laptop.zt`,
        `Aetf-Arch-VPS.zt`, `Aetf-Arch-Homelab.zt` and `haos.zt`;
    -   the application labels `auth`, `k8s`, `mon`, `photos`, `bt`,
        `files`, `dav`, `mcmap`, `login`, `sync`, `syncapi`, `matrix`,
        `test`, `haos`, `tube` and `spool`, and the SRV
        `_matrix-identity._tcp`.
-   **A primary-zone name no application owns**: rfc-003 §4.5, held by
    `test_a_name_no_application_owns_is_published_by_nobody` in
    `tests/test_dns_records.py`. In `unlimited-code.works`: `login`,
    `k8s`, `test`, `files`, `mcmap` and `archvps.stats`.
-   **A primary-zone `*.zt` name whose device has no
    `conventions.overlay.ROSTER` entry**: the overlay block publishes one
    name per roster member and nothing else (`kluster-ops#510`, ruled on
    2026-10-08). In `unlimited-code.works`: `Aetf-Laptop.zt`.
-   **A record Aetf/dns removed after `e865032`**, which its own push
    had already deleted from Cloudflare. In `jiahui.id`: the apex `A`
    and the `www` `A`, which addressed a Google Site that no longer
    exists (Aetf/dns `47ab103`, `kluster-ops#702`). No `up` deletes one:
    the pinned provider's delete reports Cloudflare's not-found answer
    as a failure, and only its read drops a record Cloudflare no longer
    holds from state. The refresh of the two that physical/gateway-cutover.md
    §8.2 runs before B6 takes them out of state, so neither B6 nor the
    `up` prints them.

**The website co-host, `unlimitedcodeworks.xyz`, has none**
(`kluster-ops#202`): the overlay block and the legacy anchor rfc-003
§5.3 keeps out of it were never live there, so its state held neither.

B6, taken before the `up` and after that refresh, printed every record
above but the ones already gone live, and the `up`'s deletes were the
same set. From then on, a line it prints is a record in state that the
program no longer declares, which the next `up` deletes.

**N2. adguardhome-sync may stop at any time** (operator ruling,
`kluster-ops#505`). It carries more than the rules written by hand, which
end with `kluster-ops#507`: alice's whole configuration but its DHCP,
so also the block `bili-cdn-probe` writes into alice's `user_rules`
and every setting outside `user_rules` (upstreams, filter lists, clients;
the census on `kluster-ops#508`). Stopping it leaves bob with what it
last copied of those until they are declared. It must stop before the
stack first writes bob, since it would overwrite bob's list with
alice's. migration.md §4 removes its unit in Wave F at the latest.

**N3. After the cutover window nothing runs `deploy.sh`.** Its
`rsync` calls carry `--delete` into `/data/on_boot.d/`,
`/data/custom/units/` and both `nspawn` directories, which would
overwrite the declared files. The daily `check-gw` runs
`deploy.sh --check`, which would report the declared layout as drift
and tell its reader to push the old one back, so the window's step 0
stops it (physical/gateway-cutover.md §4), and `check-gateway` starts in
its place at the last check of physical/gateway-cutover.md §5.

**N4. An application flips in two merges, its land and its cut**
(declarative/dns.md §6, migration.md §0 rule 1). The land wires its
component with its rows on `internet` held by the legacy VPS, so the
application runs and is verified at the cluster's front door while its
name still resolves to the legacy side; the cut deletes its `legacy.py` block and
takes the wrapper off its rows. The cut's run hands each name from
`dns`, which deletes the legacy record, to `apps`, which creates the
application's, in that order: `deploy.yml` runs `up-k8s-base` and
`up-apps` after `up-dns`, whatever its result (framework/ci.md §3).

## 3. What keeps an unapplied stack unapplied

Merging to `main` is an apply for the stacks the CI chain deploys
(framework/ci.md §3), and every merge runs that chain: `deploy.yml` is
enabled, by physical/gateway-cutover.md §8.3, and
`gh api repos/Aetf/kluster/actions/workflows/deploy.yml --jq .state`
answers `active`. So the interim rule's "never applied" rests, stack by
stack, on something other than the merge:

-   **`dns`:** nothing, since it has flipped (R1): every merge applies
    it. Nothing stops the chain there: physical/gateway-cutover.md
    §8.3's sync has put a ZeroTier identity in each of the `dns`,
    `physical` and `physical-plan` Environments (G3), without which the
    chain stops at `plan-physical`, and the `dns` Environment has no
    protection rule (G2). No reviewer is added (operator ruling,
    `kluster-ops#505`).
-   **`physical`:** the required reviewer on the `physical` Environment
    (G2; framework/ci.md §3).
-   **`k8s-base` and `apps`:** nothing at the stack: each holds a copy
    of the cluster credential in its configuration (operations.md §2.5),
    so every merge applies both, and their Environments take no
    reviewer (`kluster-ops#615`). `k8s-base` has flipped (R11). The rows
    of `apps` flip by merge (R12), so what keeps an application that has
    not migrated unapplied is that its component is not wired: it merges
    with no program calling it, which declares nothing and changes
    nothing live (cluster/migration.md §0 rule 1).
-   **`state-backend` and `github`:** operator stacks, which no job runs
    (framework/pulumi.md §3.3).

A held pull request carries the rule by hand today: Aetf/kluster#454
(R2).

## 4. Taking the readings again

The script reads only. Nothing below runs `pulumi up`, `pulumi refresh`,
`pulumi import` or `dnscontrol push`, or sends an HTTP request other
than a GET, and each group says how its commands are known to read
only. It is pasted from the primary checkout, which holds
`.credentials/`.

The readings of 2026-10-04 departed from it in two places:

-   **Z1 was skipped.** It needs root, and Z3 reads what it would.
-   **Z ran under the stack passphrase.** The readings predate
    `physical`'s passphrase of its own, which took over its
    configuration on 2026-10-08 (`kluster-ops#487`, Aetf/kluster#446),
    so Z ran without the `env -u … _FILE=…` prefix of the `physical()`
    function below. That function runs as written: the stack passphrase
    no longer decrypts `Pulumi.physical.yaml`.

The console reads (O1, O2, U5) were not taken.

```sh
setopt interactivecomments 2>/dev/null
cd ~/kluster
B="$(cat .credentials/state-backend/backend-url)"; : "${B:?the backend-url slot is empty}"
pq() { mise x -- env PULUMI_BACKEND_URL="$B" PULUMI_CONFIG_PASSPHRASE=not-the-passphrase PULUMI_SKIP_UPDATE_CHECK=1 pulumi "$@"; }
```

`env` runs inside `mise x` because `mise.toml`'s `[env]` sets
`PULUMI_CONFIG_PASSPHRASE` from the slot, over any outer value, and the
`libpq` variables come from that same `[env]`. The dummy is the
falsifier for "nothing is decrypted": a decryption fails with
`incorrect passphrase` instead of succeeding.

#### B — the state backend

```sh
# B1 every stack in the backend, its resource count, its last write
pq stack ls --all --json | jq -r '.[] | [.name, "resources=\(.resourceCount // 0)", "written=\(.lastUpdate // "-")", (if .updateInProgress then "UPDATE IN PROGRESS" else "" end)] | @tsv'

# B2 the whole history of each stack, classified
for s in dns github physical k8s-base apps; do
  pq stack history --stack "$s" --json --page-size 1000 | jq -r --arg s "$s" '
    def actor: .environment as $e
      | (if ($e["ci.system"] // "") == "" then "local"
         else "ci " + ($e["ci.build.url"] // $e["ci.system"]) end)
      + (if $e["exec.agent"] then " agent=" + $e["exec.agent"] else "" end)
      + " @" + (($e["git.head"] // "?")[0:12])
      + (if $e["git.dirty"] == "true" then "+dirty" else "" end)
      + (if $e["vcs.root"] then " root=" + $e["vcs.root"] else "" end);
    def changes: (.resourceChanges // {}) | to_entries | map("\(.key)=\(.value)") | join(",");
    (. // []) as $h
    | ($h | map(select(.kind == "update" or .kind == "destroy"))) as $w
    | "\($s): " + (if ($h | length) == 0 then "no history"
        elif ($w | length) == 0 then "never applied (" + ($h | map(.kind) | unique | join(",")) + ")"
        else "applied or attempted: last \($w[0].kind) \($w[0].result) \($w[0].startTime) by \($w[0] | actor)" end),
      ($h[] | "    \(.startTime)  \(.kind)  \(.result)  \(actor)  \(changes)")'
done

# B3 the dns resources by type
pq stack export --stack dns | jq -r '.deployment.resources[].type' | sort | uniq -c | sort -rn

# B4 the committed state-backend checkpoint, which no backend holds
F=checkpoints/.pulumi/stacks/kluster-py/state-backend.json
git show main:"$F" | jq -r '.checkpoint.latest | "resources=\(.resources // [] | length) pending=\(.pending_operations // [] | length) written=\(.manifest.time) cli=\(.manifest.version)"'
git log main --format='%h %ad %an  %s' --date=short -- "$F"
cmp -s <(git show main:"$F") "$F" && echo "the working copy holds main's checkpoint" || echo "the working copy holds a run main lacks"

# B5 whose box serves the state
A="$(mise x uv -- uv run python -c 'from kluster.lib.state_backend import settings as s; print(f"{s.ADDRESS}:{s.PORT}")')"
openssl s_client -starttls postgres -connect "$A" </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256
mise x uv -- uv run python -c 'import yaml; print(yaml.safe_load(open("Pulumi.state-backend.yaml"))["config"]["kluster-py:serverCertificate"])' | openssl x509 -noout -fingerprint -sha256

# B6 the dns records in state that the program does not declare: the next up's deletes (note N1)
pq stack export --stack dns | mise x uv -- uv run python -c 'import json, sys; from kluster.conventions import ALL_ZONES; from kluster.components.dns import LEGACY, base, zone_records; b = (*base.blocks(anchors=base.AnchorAddresses("", "", "")), *LEGACY); d = {f"{z}-{r.resource_key}" for z in ALL_ZONES for r in zone_records(z, b)}; s = [(r["urn"].rsplit("::", 1)[1], r.get("inputs", {})) for r in json.load(sys.stdin)["deployment"]["resources"] if r["type"] == "cloudflare:index/dnsRecord:DnsRecord"]; print(*sorted("\t".join((n, i.get("name", "?"), i.get("type", "?"))) for n, i in s if n not in d), sep="\n")'
```

-   **B1** lists checkpoints and cannot write. `resourceCount` counts
    every resource in the checkpoint, the root stack and the providers
    included. `lastUpdate` is the checkpoint's `manifest.time`, which
    any write moves, an import included, so it is not an apply date
    (`pkg/backend/diy/stack.go` at the pinned CLI). `state-backend` is
    absent by design (B4).
-   **B2** loads each stack load-only, and sets up decryption only under
    `--show-secrets` (`pkg/cmd/pulumi/stack/stack_history.go`). That
    flag is omitted, because its path can also rewrite the stack file.
    The page size is set because the default is ten and the backend
    pages newest first.
    -   Reading the kinds: `update` and `destroy` write the live system,
        and a `failed` one may have written part of it. `resource-import`
        and `refresh` write state alone.
    -   A `pulumi stack import` records nothing in this backend:
        `ImportDeployment` in `pkg/backend/diy/backend.go` saves the
        checkpoint and no history. So resources in B1 with no history in
        B2 mean a checkpoint carried in that way, with nothing from
        before it readable here.
    -   The backend records no user or host. `local` is any run that is
        not CI, a workstation's or an agent session's, and `agent=` names
        the agent where the entry carries `exec.agent`. The CLI sets that
        key from its own environment unless the hidden `--exec-agent` flag
        names one (`addExecutionMetadataToEnvironment` in
        `pkg/cmd/pulumi/metadata/metadata.go`, `Detect` in
        `sdk/go/common/util/agentdetect/agentdetect.go`, at the pinned
        CLI): `AI_AGENT`, trimmed and lowercased, which is how a Claude
        Code session records `claude-code_<version>_agent`, and otherwise
        the name of the first agent whose marker variable is set, `claude`
        for `CLAUDECODE`.
        That detection arrived in Pulumi 3.230.0 (its changelog,
        `pulumi/pulumi#22497`); an earlier CLI records the key from the
        flag alone. Every version `mise.toml` has pinned, from 3.257.0 on,
        is past it and reads both variables. Nothing this repository runs
        passes the flag: its workflows and scripts call the CLI itself. So
        `agent=` is the CLI's own detection from the run's environment,
        and its absence means only that none was recorded: an agent whose
        variables did not reach the CLI's environment leaves no key, and
        neither does a run by an earlier, unpinned CLI.
    -   `git.head` is the commit at `HEAD` of the repository Pulumi finds
        by walking up from the program's directory to the first entry
        named `.git`, and `git.dirty` is whether `git status` run in that
        directory prints anything
        (`GetGitRepository` in `sdk/go/common/util/gitutil/git.go`,
        `addGitCommitMetadata` in `pkg/cmd/pulumi/metadata/metadata.go`).
        Which commit that is depends on where the run started. `jj`
        keeps each git `HEAD` at the `@-` of the working copy it belongs
        to, as of the last `jj` command that updated that working copy,
        which a snapshot alone does not (framework/dispatch.md §1.2):
        -   From the primary checkout, `git.head` is the primary's `@-`.
        -   From a workspace that is a git worktree, which is what
            `jj workspace add` run in the primary makes, the walk stops
            at the workspace's own `.git` file, and `git.head` is that
            workspace's `@-`, not the primary's commit. Work still in the
            workspace's `@` is named by no key; it shows as `+dirty`.
        -   From a workspace that is no worktree, the walk reaches the
            primary's `.git`: `git.head` is the primary's `@-`, and the
            `git status` behind `+dirty` answers about the primary's
            tree, whose `.gitignore` covers the workspace. Its own work,
            committed or not, is named by no key and does not show as
            `+dirty`.

        `git.headName` is absent in each case, since that `HEAD` is
        detached. `vcs.root`, which B2 prints as `root=`, tells the last
        case apart: it is the program's path from the root git found, `.`
        from the primary or a worktree and `.claude/workspaces/<name>` from
        a workspace that is no worktree. These keys, `exec.agent`
        included, are in the history entry alone: the stored deployment
        (B3) carries none of them.
-   **B3:** `stack export` prints the stored deployment. It
    deserializes, which is where decryption happens, only under
    `--show-secrets` (`stack_export.go`).
-   **B4:** `git show` and `git log` read objects, and `cmp` reads
    files. The stack's `history/` never leaves the machine that ran it
    (framework/pulumi.md §3.3), so the landed commits are its record.
-   **B5** is a TLS handshake and a local parse. Equal fingerprints mean
    the stack's box serves; different ones mean a box serves under a
    certificate the stack's configuration does not hold, as the box the
    appliance's earlier script built did.
-   **B6** is B3's export, which decrypts nothing, piped into a local
    parse. The declared set is the program's own derivation: the blocks
    `stacks/dns.py` declares, `base.blocks` and `LEGACY`, through
    `zone_records`, each record named as `ManagedZone` names it,
    `<zone>-<key>`. The anchors' addresses are left empty, because a
    record's name does not depend on its content. What it prints is one
    line per record in state outside that set: the state name, then the
    name and type the state's inputs hold, or `?` where they hold none.
    It errs loudly rather than quietly: were `ManagedZone`'s naming to
    move without B6, every record in state would print. It was not among
    the readings of 2026-10-04; it was taken before the `dns` first up
    (note N1).

#### C — Cloudflare through DNSControl

```sh
# C0 the token, DNSControl's last declaration, and the binary the Aetf/dns push workflow ran
h="$(sha256sum Pulumi.dns.yaml)"; T="$(mise x -- pulumi config get cloudflareApiToken --stack dns)"; [ "$h" = "$(sha256sum Pulumi.dns.yaml)" ] && echo "stack file unchanged"
D=~/.cache/ops505-dnscontrol; mkdir -p "$D"
[ -d "$D/dns" ] || git clone --quiet https://github.com/Aetf/dns "$D/dns"; git -C "$D/dns" fetch --quiet && git -C "$D/dns" checkout --quiet --detach 47ab103
curl -sSLo "$D/dnscontrol" https://github.com/StackExchange/dnscontrol/releases/download/v3.31.4/dnscontrol-Linux
echo "054d236531df2674c9286279596f88f02c1cf7b1448dc5f643f1a1dbe705fe8d  $D/dnscontrol" | sha256sum -c - && chmod +x "$D/dnscontrol"

# C1 DNSControl's last declaration against the live zones: what the way back would change
(cd "$D/dns" && CLOUDFLARE_API_TOKEN="$T" "$D/dnscontrol" preview --expect-no-changes); echo "exit=$?"

# C2 CAA at each authoritative server, DS at each parent
for z in $(mise x uv -- uv run python -c 'from kluster import conventions as c; print(*c.ALL_ZONES)'); do
  ns="$(drill NS "$z" | awk '/^;/ {next} $4 == "NS" {print $5; exit}')"
  printf '%s\tCAA: %s\tDS: %s\n' "$z" \
    "$(drill CAA "$z" @"$ns" | awk '/^;/ {next} $4 == "CAA" {$1 = $2 = $3 = $4 = ""; printf "%s;", $0}')" \
    "$(drill DS "$z" | awk '/^;/ {next} $4 == "DS" {printf "%s/%s;", $5, $6}')"
done
```

-   **C0** is the one place in the script that decrypts, with the real
    stack passphrase, which `mise` supplies here. `config get` reads one
    secret and rewrites the stack file only when the secrets manager's
    state changes (`pkg/cmd/pulumi/config/config.go`, `getConfig`),
    which the surrounding checksum would show. `47ab103` is the commit
    before Aetf/dns's pointer commit, the last that holds `dnsconfig.js`
    (note N1). The release and its digest are the ones
    `koenrh/dnscontrol-action@v3` installed for the Aetf/dns push
    workflow.
-   **C1:** in that release both of `preview`'s writes, the zone create
    and each correction, run only under `push`
    (`commands/previewPush.go`). `--expect-no-changes` makes any
    correction a non-zero exit, and `--notify` stays off. It writes one
    local file, `spfcache.updated.json`, in the clone. On 2026-10-04, at
    `e865032`, it reported no correction on any zone. Since the `dns`
    first `up`, its corrections are what the way back of note N1 would
    apply.
-   **C2** is DNS queries, through `drill`.

#### X — the DKIM key, in all three places

```sh
kubectl config current-context
kubectl get secret -A --field-selector metadata.name=cert-dkim-exim -o jsonpath='{.items[0].data.tls\.key}' | base64 -d | openssl pkey -pubout -outform DER | sha256sum
for z in $(mise x uv -- uv run python -c 'from kluster.components.dns.base import MAIL_ZONES; print(*MAIL_ZONES)'); do
  ns="$(drill NS "$z" | awk '/^;/ {next} $4 == "NS" {print $5; exit}')"
  drill TXT "k8s._domainkey.$z" @"$ns" | awk '/^;/ {next} $4 == "TXT" {$1 = $2 = $3 = $4 = ""; print}' | tr -d '" \n' | sed 's/.*p=//' | base64 -d | sha256sum
done
mise x uv -- uv run python -c 'from kluster.conventions import DKIM_K8S as k; print(k.split("p=", 1)[1])' | base64 -d | sha256sum
```

One digest for the key the legacy relay signs with, one for the TXT in
each mail zone, and one for `DKIM_K8S`; on 2026-10-04 they were one
digest, so the DKIM key is in no row's drift. `kubectl get` is a read
verb, `drill` a query, and the last line a constant.

In the new cluster the key is the Secret `dkim-exim`
(`conventions.sealed.DKIM_EXIM`), which `credentials derived dkim-exim
record` seals from the legacy one after refusing any other key. At
exim's wave (cluster/migration.md §2), with the new cluster's context,
its line gives the same digest as the legacy Secret's:

```sh
kubectl get secret -A --field-selector metadata.name=dkim-exim -o jsonpath='{.items[0].data.tls\.key}' | base64 -d | openssl pkey -pubout -outform DER | sha256sum
```

#### U — the gateway and gw-config

```sh
# U1
~/.config/gw-config/deploy.sh --check
# U2
ssh gw 'machinectl list --no-legend; systemctl is-active udm-boot.service'
# U3
ssh gw 'for i in alice bob; do f=/data/adguard-$i/AdGuardHome.yaml; echo "$i rewrites_enabled=$(sed -n "s/^ *rewrites_enabled: //p" $f) rewrites=$(grep -c "^    - domain:" $f) dnsrewrite_rules=$(grep -c dnsrewrite $f)"; done'
# U4
ssh gw 'grep -c -E "mirrorakam|layer4" /data/caddy/config/caddy/Caddyfile'
```

-   **U1** is gw-config's dry run, the one its daily `check-gw` timer
    runs. Its `--check` branch runs `rsync -n` and `ssh gw cat`/`stat`,
    and exits before the first write of its apply branch (`deploy.sh`).
-   **U2–U4** are read verbs over SSH.
-   **U5** is the controller, read in its console: the bridged provider
    is this repository's only client of the controller's API, and a
    `physical` preview refuses without the B2 keys. It reads whether
    Networks lists a cluster VLAN object, whether the zone list has a
    cluster zone, and, in Port Forwarding and the UPnP leases, what holds
    the peer port.

#### K — the legacy cluster through kluster-code

```sh
# K1
(cd ~/kluster-code && mise x -- env PULUMI_CONFIG_PASSPHRASE=not-the-passphrase pulumi stack history --stack dev --json --page-size 5 | jq -r '.[] | "\(.startTime)  \(.kind)  \(.result)  \(.environment["ci.build.url"] // "local")  @\((.environment["git.head"] // "?")[0:12])"')
# K2
gh api repos/Aetf/kluster-code/commits/main --jq '.sha[0:12]'
# K3
kubectl get nodes -o wide
```

-   **K1** reads the legacy backend that kluster-code's `mise.toml`
    names, the same way B2 reads this one. In kluster-code the merge is
    the apply, so its last `update` at K2's head means `main` is
    applied.
-   **K3** shows which cluster the ambient context reaches.

#### G — GitHub

```sh
# G1
mise x uv -- uv run operator-stack github plan; echo "exit=$?"
# G2
gh api repos/Aetf/kluster/environments --jq '.environments[] | "\(.name)\t\([.protection_rules[]?.type] | join(","))"'
# G3
for e in dns physical physical-plan; do printf '%s: ' "$e"; gh api "repos/Aetf/kluster/environments/$e/secrets" --jq '[.secrets[].name] | join(" ")'; done
# G4
R="$(gh run list --repo Aetf/kluster --workflow deploy.yml --limit 1 --json databaseId --jq '.[0].databaseId')"; gh run view "$R" --repo Aetf/kluster --json jobs --jq '.jobs[] | "\(.name)\t\(.conclusion)"'
# G5
gh api repos/Aetf/dns --jq .archived; gh api repos/Aetf/dns/commits/master --jq '.sha[0:12] + " " + (.commit.message | split("\n")[0])'; gh api repos/Aetf/dns/actions/secrets --jq '[.secrets[].name] | join(" ")'
# G6
gh run list --repo Aetf/kluster-code --workflow pulumi-deploy.yml --limit 3
```

-   **G1** reads drift in the forge. `plan` is `pulumi preview
    --refresh` (framework/pulumi.md §3.3). A preview against this
    backend takes no lock, keeps no snapshot and records no history: in
    `pkg/backend/diy/backend.go`, `Preview` locks nothing, and `apply`
    persists only when the run is not a dry run. The refresh is reads at
    GitHub. It decrypts `githubAdminToken` under the operator
    passphrase, which the driver acquires.
-   **G2–G6** are GET requests (`gh api` sends one unless given a field) and
    `gh run` reads.
-   **G3**, and **G5**'s last command, list secret names, never values.
    A `ZEROTIER_IDENTITY` on `dns` and on `physical-plan` means the next
    push to `main` applies `dns` (§3).

#### H — the homelab host

```sh
# H1
(cd ~ && GIT_OPTIONAL_LOCKS=0 yadm status --porcelain -- .config/gw-config .config/aconfmgr .config/containers/systemd .config/systemd/user)
# H2
systemctl list-units --all --no-pager 'qbittorrent-nox@*' 'k3s*'; systemctl --user list-units --all --no-pager 'adguardhome-sync*' 'seedwatch*' 'thread-dashboard*' 'kluster-state-backend*' 'check-gw*' 'gw-backup*' 'check-gateway*' 'gateway-backup*' 'dmarc-check*'
# H3
virsh -r -c qemu:///system list --all; virsh -r -c qemu:///system pool-list --all --details
# H4
ip -6 -brief address show | awk '$1 ~ /^zt/'
```

-   **H1** prints nothing when every tracker edit on the host is
    committed. `GIT_OPTIONAL_LOCKS=0` keeps `status` from rewriting the
    index.
-   **H2** is a read verb. Its patterns are quoted, since `nullglob`
    in `zsh` drops an unquoted glob that matches no file.
-   **H3:** `-r` opens a read-only connection. No worker domain and no
    pool on the `nodatacow` subvolume may exist before `physical` is
    applied (physical/homelab-host.md §4).
-   **H4** is the homelab member's own view of the IPv6 addresses
    Central's RFC 4193 and 6PLANE modes derive from the network id and
    its node id: one `fd`-prefixed address for the first, one
    `fc`-prefixed for the second, beside the link-local. It stands in
    for Z2's `v6` until Z2 is taken again, and it says nothing about
    other members.

#### Z — ZeroTier Central

```sh
# Z1
sudo zerotier-cli -j listnetworks | jq '.[] | {nwid, name, assignedAddresses, routes, dns}'
physical() { (cd ~/kluster && mise x -- env -u PULUMI_CONFIG_PASSPHRASE PULUMI_CONFIG_PASSPHRASE_FILE=.credentials/physical.passphrase pulumi "$@" --stack physical); }
h="$(sha256sum Pulumi.physical.yaml)"; ZT="$(physical config get zerotierApiToken)"; [ "$h" = "$(sha256sum Pulumi.physical.yaml)" ] && echo "stack file unchanged"
N="$(mise x uv -- uv run python -c 'from kluster.conventions import overlay; print(overlay.NETWORK_ID)')"
# Z2
printf 'Authorization: token %s\n' "$ZT" | curl -sS -H @- "https://api.zerotier.com/api/v1/network/$N" | jq '{name: .config.name, rulesSource, routes: .config.routes, dns: .config.dns, pools: .config.ipAssignmentPools, v6: .config.v6AssignMode}'
# Z3
printf 'Authorization: token %s\n' "$ZT" | curl -sS -H @- "https://api.zerotier.com/api/v1/network/$N/member" | jq -r '.[] | [.name, .nodeId, .config.authorized, (.config.ipAssignments // [] | join(","))] | @tsv' | sort
# Z4
mise x uv -- uv run python -c 'from kluster.conventions import overlay; [print(m.name, getattr(m, "node_id", None) or "-", m.address, sep="\t") for m in overlay.ROSTER]' | sort
```

-   **Z1** is the homelab member's own view, through a list verb.
-   `physical()` is physical/gateway-cutover.md's function. Its
    `config get` decrypts one value under `physical`'s passphrase,
    guarded the way C0 is.
-   **Z2 and Z3** are GET requests to Central's v1 API, at the host and
    with the `token` header that ZeroTier's documentation of that API
    gives, the form the runbooks of physical/gateway.md and
    physical/gateway-cutover.md send. The token reaches `curl` on its
    standard input, where the shell's own `printf` writes it, rather than
    on its command line.
-   Z3 against Z4 says whether Central agrees with the roster.

#### O — OCI and B2, in their consoles

Neither has a read-only client among the tools `mise.toml` pins.

-   **O1:** in the home region, compartment `kluster-physical` holds no
    instance, network, load balancer or volume, and compartment `kluster`
    holds the appliance.
-   **O2:** B2 lists no bucket `aetf-kluster-backup`.
