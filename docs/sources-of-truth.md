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
taken on 2026-10-04. N1 to N4 are the notes that follow the table.
Column 5 names issues in the ops repository, where the drift is tracked.

| Row | Domain | (1) Live source of truth today | (2) Stack · apply state | (3) Tracker until the flip · what "take" means | (4) Flip → retirement | (5) Known drift |
|---|---|---|---|---|---|---|
| R1 | `dns` · the Cloudflare zones: base records, mail with DKIM, the `*.zt` block, `legacy.py`'s application records, anchors, CAA, DNSSEC | DNSControl: Aetf/dns `dnsconfig.js`, whose `push.yml` is active and last applied on 2026-10-04 (G5). Its preview reports no correction on any zone at `e865032` (C1). The ruling that keeps it authoritative is `kluster-ops#177` | `dns` · **imported, never applied.** Its only update is a `resource-import` that succeeded on 2026-08-26, run locally (B2). It holds 139 resources (B1): 123 `DnsRecord`, 6 `Zone`, 6 `ManagedZone`, 2 `ZoneDnssec` (B3). The repository-against-live preview cannot run before `physical` is applied (note N1), and the stack's refresh fails against Cloudflare (`kluster-ops#511`) | Aetf/dns: edit `dnsconfig.js` and merge to `master`, and the merge applies (`push.yml`, no gate). Here: the same edit to `components/dns/`, same sitting, unapplied; the DKIM key change (Aetf/kluster#441) is the shape | `kluster-ops#75`'s `dns` first up, in the order of note N1 → `kluster-ops#7` (the pointer commit, then the archive) | `kluster-ops#510`; `kluster-ops#511`; `kluster-ops#478` |
| R2 | `dns` · the split-horizon answers on alice and bob | alice, by hand. adguardhome-sync, a homelab user unit that is running (H2), copies alice to bob. Both instances answer from `$dnsrewrite` user rules, 18 each, with the rewrite list the stack writes empty and switched off (U3) | `dns` · declares none: B3 lists no rewrite among the stack's types. Its first rows are Aetf/kluster#408, open and held | alice's UI, which the sync carries to bob, and the weekly backup pull records in yadm. Here: every rule the instances answer from, declared in the `user_rules` list of each instance, a list one resource per instance owns whole (`kluster-ops#507`, way (b)), same sitting, unapplied. The `lan.ucw.phd` rules are declared too until their application migrates (rule 4), since the first apply of that list removes every rule it does not declare | Aetf/kluster#408, rebuilt on the user-rules list, merged and applied after R1's flip; adguardhome-sync stopped before it (note N2) → its unit's removal (`kluster-ops#79`) | `kluster-ops#507`; `kluster-ops#482`; `kluster-ops#223`; `kluster-ops#143` |
| R3 | `physical` · the cloud fleet in compartment `kluster-physical` (network, nodes, load balancer, addresses, volumes, guardrails) and the Talos day-1 chain | none: never applied | `physical` · **no history**: 0 resources, the stack initialized on 2026-08-26 and never updated (B1, B2). The console read of OCI (O1) was not taken | none | `physical`'s run with no targets in `kluster-ops#75` (physical/gateway.md §2.5) → none | none known |
| R4 | `physical` · the homelab's libvirt objects: the pool on the `nodatacow` subvolume, and the worker's volume, seed and domain | none. The host preparation under them is aconfmgr's, and applied (physical/homelab-host.md, status) | as R3. The host holds one domain, `haos`, and one pool, `images` (H3): no worker and no cluster pool | none for these. The host preparation stays aconfmgr's for good, and a pool defined there would fail the first apply (physical/homelab-host.md §4) | the same run as R3, then the host-side `truncate` and `virsh blockresize` (physical/homelab-host.md §1) → none | none known |
| R5 | `physical` · the gateway's device state: boot chain, units, `nspawn` settings and machines, `caddy`, AdGuard's initial state, authorized keys, device secrets, root filesystems. FRR's configuration and the ZeroTier machine are new | gw-config, yadm's `~/.config/gw-config`, pushed by hand with `deploy.sh`; root filesystems pushed by homelab-containers. Its dry run passes against the device (U1), which runs `adguard-alice`, `adguard-bob` and `caddy` under an active `udm-boot` (U2) | as R3 | gw-config: edit, `./deploy.sh`, the restart it prints, `yadm commit`. Here: the same change in `components/gateway/` or `conventions.gateway`, same sitting, unapplied | the window of physical/gateway-cutover.md, after which gw-config is frozen (note N3) → physical/gateway-cutover.md §7 after the soak; `kluster-ops#79` | `kluster-ops#508`: the live `Caddyfile` carries the Bilibili steering (U4), which nothing here declares |
| R6 | `physical` · the controller census of declarative/physical.md §4: the cluster VLAN's network, its zone, the zone policies and their order, the v6 pinhole, the v4 peer-port forward, static host entries | the controller, hand-kept in its console. `physical` has never been applied, so nothing in the census was created by it. The peer port's WAN side is the legacy qbittorrent's (physical/gateway.md §4.2) | as R3. The console read (U5) was not taken | none: the census creates its objects and adopts none that exist. What it leaves to the console stays there, recovered from the UniFi autobackup (physical/gateway-cutover.md §7) | the window's targeted run, after the pre-window probes (physical/gateway-cutover.md §3); the pinhole at physical/gateway.md §2.5's step 3; the forward in Wave D (`qbittorrentOnWorker`, migration.md §2) → none | none known |
| R7 | `physical` · ZeroTier Central: routes, flow rules, managed DNS, members | Central, hand-kept in its console (physical/gateway.md §2.1). It holds the route `10.144.0.0/16`, default rules and no managed DNS (Z2), and its members match `conventions.overlay.ROSTER`, which also lists `ci-dns` and `ci-physical`, not yet created (Z3, Z4) | as R3; the network enters state in the window's targeted run. The managed DNS of `kluster-ops#69`'s first slice is merged and not live (Z2) | none. "Take" is the Central change plus the same change to `conventions.overlay` (`ROSTER`, `MANAGED_ROUTES`), same sitting: the converge deletes a route the census lacks, and admits no member the roster lacks (physical/gateway.md §2.1–2.3) | the window's targeted run adopts the network at Central's values, and the run with no targets converges it (physical/gateway.md §2.5) → hand edits stop | `kluster-ops#510`: the roster is right, and Aetf/dns's `*.zt` block is stale against it (Z3–Z5) |
| R8 | `physical` · the B2 bucket `kluster-backup` and its keys | none | as R3. The console read of B2 (O2) was not taken | none | the run with no targets in `kluster-ops#75`, after `credentials derived b2-management mint` (credentials.md) → none | none known |
| R9 | `state-backend` · the appliance: compartment `kluster` (network, subnet, reserved address, image, image bucket, instance), the B2 dump bucket and dump key | the box the `state-backend provision` script built: the live server certificate is not the one the stack's configuration holds (B5). Aetf/kluster#414 removed the write path from the script (physical/state-backend.md, status) | `state-backend` · **no history**: its committed checkpoint holds 0 resources, initialized on 2026-09-30 (B4). No backend holds it, by design | none: frozen, since nothing writes the box until the flip. `dump`, `restore`, `bundle`, `probe` and `ssh` change nothing declared | the live drill of rfc-006 §14 slice 6 (`kluster-ops#462`), which disables `deploy.yml` from before its dump to after its restore → slice 7 removes the adoption | none known. `kluster-ops#484` blocks the flip |
| R10 | `github` · repositories, Environments, gates, protection, labels, variables | the `github` stack. Apps, installations, secret values and account roots are console state by design (framework/github.md §4) | `github` · **applied locally**, as framework/github.md §1 has it. 18 resources (B1). The last `update` succeeded on 2026-10-01; earlier ones ran on 2026-09-25 and 2026-09-18, after a `resource-import` on 2026-09-05, and on 2026-09-25 and 2026-10-01 a failed update came minutes before the one that succeeded (B2). Its plan finds every resource the same (G1) | none: flipped | flipped → none | none known. No schedule runs G1 (framework/github.md §1) |
| R11 | `k8s-base` · everything cluster-scoped in the new cluster | none: its cluster does not exist. The legacy counterpart is kluster-code's `BaseCluster`, in the legacy cluster, which the ambient `kubectl` context reaches (K3) | `k8s-base` · **no history**: 0 resources, initialized on 2026-10-01 and never updated (B1, B2) | none. What crosses from the legacy cluster is its sealing key, imported by hand at the first ported manifest (rfc-007 §6.3) | its first `up` in M2 (`kluster-ops#77`), after the copy of the cluster credential (operations.md §2.5) → the legacy base retires with kluster-code (`kluster-ops#79`) | none known |
| R12 | `apps` · each application: namespace, workload, storage, routes, public records | per application, the legacy cluster, written by kluster-code's `dev` stack, where the merge applies. Its last `update` succeeded on 2026-10-04 from CI, at the head of kluster-code's `main` (K1, K2), and its deploy workflow is succeeding (G6). The applications' public records are R1's | `apps` · **no history**: 0 resources, initialized on 2026-10-01 and never updated (B1, B2) | kluster-code: edit, merge. Here: nothing until the application migrates, except its records, which follow R1: DNSControl until R1 flips, `legacy.py` after | per application, its wave's stop-copy-start (migration.md §0 rule 1, §2): the pull request that deletes its `legacy.py` block and adds its route row and component (declarative/dns.md §6) (note N4) → its kluster-code component removed after verification; `kluster-ops#79` | `kluster-ops#509` |
| R13 | `apps` · the host services that move into the cluster: qbittorrent-nox and seedwatch (Wave D), thread-dashboard (Wave B) | the homelab host: `qbittorrent-nox@aetf` (aconfmgr), and the `seedwatch` and `thread-dashboard` user units (yadm), all running (H2) | `apps` · declares none of them | aconfmgr or yadm: edit, then `sudo aconfmgr apply`, or `systemctl --user daemon-reload` and a restart, then commit | the wave's stop-copy-start (migration.md §2) → the removal commits in yadm and aconfmgr (migration.md §0 rule 3, §4); `kluster-ops#79` | none known |

**No stack owns, now or later:** the homelab host itself under yadm and
aconfmgr, with the HAOS domain, dmarc-check and the NAS role
(migration.md §2, "not migrating"; declarative/physical.md §3); the
legacy cluster's state backend, the `kluster-state-backend` user unit,
which is running (H2) and whose Postgres stops last, in Wave F
(migration.md §4); and the controller's and the forge's console state
outside their censuses.

**N1. R1's flip.** `kluster-ops#75` runs the `dns` first up from the
operator's checkout, and `kluster-ops#7` retires Aetf/dns with a
pointer commit. **Aetf/dns takes no push from 2026-10-08 on** (operator
ruling, `kluster-ops#505`): a push to its `master` runs DNSControl over
the zones, and DNSControl removes what its file does not declare, so
one landing between the `up` and the retirement would undo the `up`.
`push.yml` stays enabled as a workflow and is not run.

The preview is reconciled line by line, as
`kluster-ops#75` asks: no replace or delete from the type move
(`kluster-ops#478`), the co-host zone's expected deletes
(`kluster-ops#202`), and the first `up`'s own changes, which are CAA and
DNSSEC on the zones that lack them, and the anchors. CAA is live on `unlimited-code.works` alone, and the
parents hold DS for `unlimited-code.works` and `unlimitedcodeworks.xyz`
alone (C2). That preview cannot run before `physical` is applied: the
program refuses until `physical` publishes the anchors' addresses
(declarative/dns.md §2).

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
overwrite the declared files. The daily `check-gw` keeps running read
only until physical/gateway-cutover.md §7 trims it.

**N4. A gap for M3's design** (`kluster-ops#120`). migration.md §0
rule 1 verifies an application before its DNS cut, while
declarative/dns.md §6 makes the deploy and the cut one merge. That
merge also hands each name from `dns`, which deletes its `legacy.py`
block, to `apps`, which creates its route, and `deploy.yml` leaves
`up-dns` and `up-apps` unordered.

## 3. What keeps an unapplied stack unapplied

Merging to `main` is an apply for the stacks the CI chain deploys
(framework/ci.md §3), so the interim rule's "never applied" rests, stack
by stack, on something other than the merge:

-   **`dns`:** the chain stops at `plan-physical`, because no
    Environment holds a ZeroTier identity: G3 finds none in `dns`,
    `physical` or `physical-plan`, and the last `deploy.yml` run failed
    there and skipped every `up` job (G4). `kluster-ops#75` runs the
    identity sync only after the `dns` first up, and that is an ordering
    in a checklist: once the sync delivers the identities, every merge
    applies `dns`, and the `dns` Environment has no protection rule
    (G2). No reviewer is added (operator ruling, `kluster-ops#505`):
    the bring-up runs the identity sync and the `dns` first up in one
    sitting, so no merge lands between them.
-   **`physical`:** the required reviewer on the `physical` Environment
    (G2; framework/ci.md §3). From the window's preparation on, the
    committed `gatewayBootstrapHost` also fails CI's `physical` jobs
    (physical/gateway-cutover.md §3).
-   **`k8s-base` and `apps`:** no copy of the cluster credential exists
    until `physical` is applied (framework/ci.md §5). After that, `apps`'
    rows flip by merge (R12), so a component for an application that
    has not migrated does not merge.
-   **`state-backend` and `github`:** operator stacks, which no job runs
    (framework/pulumi.md §3.3).

A held pull request carries the rule by hand today: Aetf/kluster#408
(R2).

## 4. Taking the readings again

The script reads only. Nothing below runs `pulumi up`, `pulumi refresh`,
`pulumi import` or `dnscontrol push`, or sends an HTTP request other
than a GET, and each group says how its commands are known to read
only. It is pasted from the primary checkout, which holds
`.credentials/`.

The readings of 2026-10-04 departed from it in two places:

-   **Z1 was skipped.** It needs root, and Z3 reads what it would.
-   **Z ran under the stack passphrase.** `physical` has no passphrase
    of its own until `kluster-ops#487`, so
    `.credentials/physical.passphrase` does not exist yet, and the
    `physical()` function below runs without its `env -u … _FILE=…`
    prefix until then.

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
    def actor: (if (.environment["ci.system"] // "") == "" then "local"
                else "ci " + (.environment["ci.build.url"] // .environment["ci.system"]) end)
      + " @" + ((.environment["git.head"] // "?")[0:12])
      + (if .environment["git.dirty"] == "true" then "+dirty" else "" end);
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
    -   The backend records no user or host. `local` is a workstation or
        an agent session. `git.head` names the primary checkout's commit
        even for a run from a `jj` workspace (framework/dispatch.md
        §1.2).
-   **B3:** `stack export` prints the stored deployment. It
    deserializes, which is where decryption happens, only under
    `--show-secrets` (`stack_export.go`).
-   **B4:** `git show` and `git log` read objects, and `cmp` reads
    files. The stack's `history/` never leaves the machine that ran it
    (framework/pulumi.md §3.3), so the landed commits are its record.
-   **B5** is a TLS handshake and a local parse. Different fingerprints
    mean the script's box serves, under a certificate of its own render
    (physical/state-backend.md, status); equal ones mean the stack's box
    serves.

#### C — Cloudflare through DNSControl

```sh
# C0 the token, and the binary the Aetf/dns push workflow runs
h="$(sha256sum Pulumi.dns.yaml)"; T="$(mise x -- pulumi config get cloudflareApiToken --stack dns)"; [ "$h" = "$(sha256sum Pulumi.dns.yaml)" ] && echo "stack file unchanged"
D=~/.cache/ops505-dnscontrol; mkdir -p "$D"
[ -d "$D/dns" ] || git clone --quiet https://github.com/Aetf/dns "$D/dns"; git -C "$D/dns" pull --quiet --ff-only
curl -sSLo "$D/dnscontrol" https://github.com/StackExchange/dnscontrol/releases/download/v3.31.4/dnscontrol-Linux
echo "054d236531df2674c9286279596f88f02c1cf7b1448dc5f643f1a1dbe705fe8d  $D/dnscontrol" | sha256sum -c - && chmod +x "$D/dnscontrol"

# C1 DNSControl against the live zones
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
    which the surrounding checksum would show. The release and its digest
    are the ones `koenrh/dnscontrol-action@v3` installs for the Aetf/dns
    push workflow.
-   **C1:** in that release both of `preview`'s writes, the zone create
    and each correction, run only under `push`
    (`commands/previewPush.go`). `--expect-no-changes` makes any
    correction a non-zero exit, and `--notify` stays off. It writes one
    local file, `spfcache.updated.json`, in the clone.
-   **C2** is DNS queries, through `drill`.

#### X — the DKIM key, in all three places

```sh
kubectl config current-context
kubectl get secret -A --field-selector metadata.name=cert-dkim-exim -o jsonpath='{.items[0].data.tls\.key}' | base64 -d | openssl pkey -pubout -outform DER | sha256sum
for z in $(mise x uv -- uv run python -c 'from kluster.components.dns.base import MAIL_ZONES; print(*MAIL_ZONES)'); do
  ns="$(drill NS "$z" | awk '/^;/ {next} $4 == "NS" {print $5; exit}')"
  drill TXT "k8s._domainkey.$z" @"$ns" | awk '/^;/ {next} $4 == "TXT" {$1 = $2 = $3 = $4 = ""; print}' | tr -d '" \n' | sed 's/.*p=//' | base64 -d | sha256sum
done
mise x uv -- uv run python -c 'from kluster.components.dns.base import DKIM_K8S as k; print(k.split("p=", 1)[1])' | base64 -d | sha256sum
```

One digest for the key the legacy relay signs with, one for the TXT in
each mail zone, and one for `DKIM_K8S`; on 2026-10-04 they were one
digest, so the DKIM key is in no row's drift. `kubectl get` is a read
verb, `drill` a query, and the last line a constant.

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
gh api repos/Aetf/dns/actions/workflows/push.yml --jq .state; gh run list --repo Aetf/dns --workflow push.yml --limit 3
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
-   **G3** lists secret names, never values. A `ZEROTIER_IDENTITY` on
    `dns` and on `physical-plan` means the next push to `main` applies
    `dns` (§3).

#### H — the homelab host

```sh
# H1
(cd ~ && GIT_OPTIONAL_LOCKS=0 yadm status --porcelain -- .config/gw-config .config/aconfmgr .config/containers/systemd .config/systemd/user)
# H2
systemctl list-units --all --no-pager 'qbittorrent-nox@*' 'k3s*'; systemctl --user list-units --all --no-pager 'adguardhome-sync*' 'seedwatch*' 'thread-dashboard*' 'kluster-state-backend*' 'check-gw*' 'gw-backup*' 'dmarc-check*'
# H3
virsh -r -c qemu:///system list --all; virsh -r -c qemu:///system pool-list --all --details
```

-   **H1** prints nothing when every tracker edit on the host is
    committed. `GIT_OPTIONAL_LOCKS=0` keeps `status` from rewriting the
    index.
-   **H2** is a read verb. Its patterns are quoted, since `nullglob`
    in `zsh` drops an unquoted glob that matches no file.
-   **H3:** `-r` opens a read-only connection. No worker domain and no
    pool on the `nodatacow` subvolume may exist before `physical` is
    applied (physical/homelab-host.md §4).

#### Z — ZeroTier Central

```sh
# Z1
sudo zerotier-cli -j listnetworks | jq '.[] | {nwid, name, assignedAddresses, routes, dns}'
physical() { (cd ~/kluster && mise x -- env -u PULUMI_CONFIG_PASSPHRASE PULUMI_CONFIG_PASSPHRASE_FILE=.credentials/physical.passphrase pulumi "$@" --stack physical); }
h="$(sha256sum Pulumi.physical.yaml)"; ZT="$(physical config get zerotierApiToken)"; [ "$h" = "$(sha256sum Pulumi.physical.yaml)" ] && echo "stack file unchanged"
N="$(mise x uv -- uv run python -c 'from kluster.conventions import overlay; print(overlay.NETWORK_ID)')"
# Z2
curl -sS -H "Authorization: bearer $ZT" "https://my.zerotier.com/api/network/$N" | jq '{name, rulesSource, routes: .config.routes, dns: .config.dns, pools: .config.ipAssignmentPools}'
# Z3
curl -sS -H "Authorization: bearer $ZT" "https://my.zerotier.com/api/network/$N/member" | jq -r '.[] | [.name, .nodeId, .config.authorized, (.config.ipAssignments // [] | join(","))] | @tsv' | sort
# Z4
mise x uv -- uv run python -c 'from kluster.conventions import overlay; [print(m.name, getattr(m, "node_id", None) or "-", m.address, sep="\t") for m in overlay.ROSTER]' | sort
# Z5
gh api -H 'Accept: application/vnd.github.raw' repos/Aetf/dns/contents/dnsconfig.js | sed -n '/var ZT_HOSTS/,/];/p'
```

-   **Z1** is the homelab member's own view, through a list verb.
-   `physical()` is physical/gateway-cutover.md's function. Its
    `config get` decrypts one value under `physical`'s passphrase,
    guarded the way C0 is.
-   **Z2 and Z3** are GET requests, on the paths and with the header that
    `zerotier/go-ztcentral`, the client under the provider, uses.
-   Z3 against Z4 and Z5 says which of the roster and DNSControl's
    `*.zt` block Central agrees with.

#### O — OCI and B2, in their consoles

Neither has a read-only client among the tools `mise.toml` pins.

-   **O1:** in the home region, compartment `kluster-physical` holds no
    instance, network, load balancer or volume, and compartment `kluster`
    holds the appliance.
-   **O2:** B2 lists no bucket `kluster-backup`.
