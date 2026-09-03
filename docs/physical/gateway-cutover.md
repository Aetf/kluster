# Gateway Cutover

The one maintenance window that hands the device over: it stops being
converged by the retiring gw-config repository and starts being converged
by this program. It is the opening of the bring-up ceremony
([gateway.md](gateway.md) §2.5) — the first push from this program is
the push this window prepares for, and what has to happen before it is
moving the live container state to where the declaration expects it.

Everything below runs as `root` in a LAN session on the device, except
where a step says otherwise. That is not a preference: there is no
overlay member yet — the member is one of the things this push delivers —
which is why the ceremony's first three steps dial the LAN.

## 1. Why there is a window

Nothing this program declares is on the device yet: the `physical` stack
has no state, so every name it declares is free, and each root filesystem
is pulled fresh by pin rather than adopted from what is there. The device
is not free. It runs three machines under the layout gw-config built, and
two kinds of state under that layout are not re-derivable:

-   **Each resolver's live configuration** — `AdGuardHome.yaml` and the
    data beside it. What this program declares for a resolver is an
    *initial* state, installed only into a state directory that has never
    held one (gateway.md §1.1), so a resolver whose state is not in place
    before the push comes up on factory configuration with the LAN's
    filters, clients and rewrites gone.
-   **The proxy's ACME account.** The declared `Caddyfile` names the
    contact the account already carries, which is the key the proxy
    loads it by; that storage is what the window carries across, and a
    different address would register a new account and leave the old one
    behind. The *certificates* in it are for the names the old proxy
    served, so a fresh issuance at first start is expected — see §5,
    which checks that the issuance **succeeded**, not that none happened.

Both are why the window covers all three machines at once: a partial move
followed by a push would install factory state into whichever machine had
not moved.

## 2. What moves, and what does not

-   **Every machine directory is replaced.** `/data/custom/machines/<name>`
    is the root filesystem tree itself today; under the declaration that
    path is the machine's own directory, holding `rootfs/`, `state/`, the
    `<name>.nspawn` settings, the digest marker and the content stamp
    (gateway.md §1.1). The whole of today's `machines/` is moved aside in
    one rename, which takes the three live trees and the three `.old`
    rollback copies the retiring push mechanism left beside them — about
    370 MB — with it. Size does not decide the cost: every move in §4 is
    a rename inside `/data`, so it is a directory entry rewritten and not
    bytes copied.
-   **The resolvers' state moves whole**: `/data/adguard-alice` and
    `/data/adguard-bob` become `machines/<name>/state`. The in-container
    path does not change — the image is started against `/data/adguard`
    either way, and it is the bind that moves.
-   **The proxy's state is the data half alone**: `/data/caddy/data`,
    which holds `caddy/acme` and `caddy/certificates`, becomes
    `machines/caddy/state` and is bound at `/var/lib/caddy`. What stays
    behind as residue is the rest of `/data/caddy` — `config/`,
    `secrets/` and `resolv.conf` — because the `Caddyfile`, the ACME
    token and the resolver file are all rendered mounted files of the
    machine now (gateway.md §1).
-   **The overlay member has nothing to move.** It is net-new on this
    device; its identity is minted by its first start, and reading it is
    the ceremony's next step (gateway.md §2.5).
-   **The old settings files are replaced, not merged.**
    `30-nspawn-units.sh` mirrors each machine's `<name>.nspawn` into
    `/etc/systemd/nspawn` and removes what has no source there, so the
    three files gw-config pushed are overwritten by the first push. They
    are removed by hand in §4 anyway, because between the stop and the
    push they are the only thing that would still describe a machine.
-   **The routing configuration is installed, the protocol daemon
    switched on, and the daemon started.** `/etc/frr/frr.conf` on the
    device today is FRR's stock file — one `log syslog informational`
    line — with the daemon inactive and `bgpd=no` in `/etc/frr/daemons`,
    so the push drops no session: this is the site's first BGP
    configuration, not a replacement for one (gateway.md §1.3). The
    toggle and the file are converged by the one executable, which
    switches `bgpd=no` to `bgpd=yes` and then asserts the result — so a
    run that reaches the restart has the protocol daemon on, or has
    already failed. Three further consequences for the window. It
    **restarts** the daemon rather than reloading it — the reload verb
    needs a helper this firmware does not ship — and a restart against
    an inactive daemon is a start. **Started is not enabled, and is not
    meant to become enabled**: nothing enables `frr.service`, so
    `systemctl is-enabled frr` answers `disabled` here and on every
    healthy day after, and is not a check of anything. What starts the
    daemon at each boot is the `Wants=frr.service` edge in
    `frr-config.service`, which `20-units.sh` enables at every boot; the
    executable's stamp lives beside the installed file in `/etc`, which
    a reboot keeps, so the first reboot of the soak finds file and stamp
    equal and exits having done nothing — with the daemon up regardless,
    because the edge started it. And **no session comes up until the
    worker VM exists** to peer with: the declared peer is dialed and
    nothing answers, which is the expected state rather than a fault.

    **The edge itself is proven only after a reboot**, which the push
    cannot show and which nothing here schedules — the device takes one
    on its own whenever an unattended firmware pass lands. So §5's
    routing block is run a second time once the soak has taken a
    reboot: `active` then is the edge working, `inactive` then is the
    edge failed. That is the one reading a healthy device cannot give,
    and the only thing about the boot path the push itself cannot show.

## 3. Before the window opens

-   **The legacy vhost census has landed.** The declared `Caddyfile`
    serves the controller console and the two resolver interfaces; the
    live one serves eleven names under `lan.ucw.phd` whose apps migrate
    in Waves B–D (cluster/migration.md §2). Those rows are carried into
    the declaration, so the cutover is not a user-visible regression
    (Aetf/kluster-ops#155, ruled; #163 builds it). Without them on the
    branch being applied, the window takes those names down for weeks.
-   **The four packages are installed already.** The push's first act is
    `10-packages.sh`, which exits without doing anything when
    `systemd-container`, `libnss-mymachines`, `skopeo` and `umoci` are
    all present, and fails the push when apt cannot reach the internet
    and the offline cache cannot satisfy them — *after* the machines have
    been stopped and moved. Installing them in advance takes that failure
    out of the window: `apt-get install -y systemd-container
    libnss-mymachines skopeo umoci`, on any day before it.
-   **The ACME token is minted and committed**: `credentials derived
    cloudflare-gateway-acme mint` has run and the `physical` stack file
    carrying the token is committed (credentials.md §3). The proxy comes
    up with no way to renew otherwise.
-   **`gatewayBootstrapHost` is set to the device's LAN address and
    committed** (gateway.md §2.5). This commit makes CI's `physical`
    jobs fail until the ceremony's last step unsets it again: continuous
    integration reaches the site over the overlay and has no route to the
    LAN, and an unreachable device fails the whole `physical` preview by
    design (gateway.md §3). Nothing applies the stack unattended in the
    meantime; the operator's session is the only one that can.
-   **The two host-side timers are stopped** — see step 0, which is
    inside the window only because it must not be forgotten.
-   **A current UniFi autobackup is in hand** (§7): unrelated to the
    machines, and the cheapest insurance in the window.

## 4. The window

Downtime is the whole of steps 1 to 3, not the renames alone: the moves
are instant, but the push that follows pulls four root filesystems from
the registry over the site's uplink and unpacks each one, which is tens
of minutes rather than minutes. The LAN has no resolver for that entire
span — every lease names alice and bob, and both are down — while the
device's own resolution is unaffected, since it goes to `127.0.0.1` and
not to the machines it hosts, so the pulls proceed regardless.

**Step 0 — stop the host-side timers**, on the homelab host, as the user
that owns them:

```sh
systemctl --user disable --now check-gw.timer gw-backup.timer
```

The daily check runs `gw-config/deploy.sh --check` and mails what it
finds with the instruction to run `deploy.sh`. The morning after the
window it would report the entire new layout as drift and tell its reader
to re-push the old boot chain and settings files over it, `--delete` and
all — and the house would lose DNS at the next boot either way. On a plain
reboot the machine links survive, and the restored settings bind
`/data/adguard-alice`, which no longer exists: a machine whose bind source
is missing is one nspawn refuses to start. On a post-firmware boot the
links are gone, and the restored `40-machines.sh` makes one to
`machines/<name>`, which is now a directory with no `/sbin/init`. The weekly pull would
likewise fail from that morning on, its `set -e` tripping over the
resolver state it can no longer find. They come back when §7's
replacements exist.

**Step 1 — stop the machines.** State directories are moved out from
under running containers otherwise:

```sh
for m in adguard-alice adguard-bob caddy; do
    systemctl disable --now "systemd-nspawn@$m.service"
    rm -f "/var/lib/machines/$m" "/etc/systemd/nspawn/$m.nspawn"
done
```

The unit is systemd's own template and the declaration instances the
same one, so nothing here contends with anything: what the stop buys is
a quiet filesystem, and the two removals are hygiene —
`40-machines.sh` would relink the one and `30-nspawn-units.sh` would
overwrite the other, and neither should be describing a machine while
the move is in flight.

**Step 2 — move the old layout aside and put the state where the
declaration reads it:**

```sh
mv /data/custom/machines /data/custom/machines-old
for m in adguard-alice adguard-bob caddy; do
    mkdir -p "/data/custom/machines/$m"
done
mv /data/adguard-alice /data/custom/machines/adguard-alice/state
mv /data/adguard-bob   /data/custom/machines/adguard-bob/state
mv /data/caddy/data    /data/custom/machines/caddy/state
```

One rename takes the old trees and their `.old` copies together. The
overlay member gets no directory here: `40-machines.sh` creates the
state directory, and an empty one is what mints a new identity.

**Step 3 — `pulumi up` the `physical` stack** from the operator's
workstation, over the LAN. It delivers the boot chain, the unit sources,
the executables, the routing configuration, the authorized key, and for
each machine its settings file, its root filesystem, its mounted
configuration and secrets, and the digest marker naming the pin its tree
came from. The initial states are no-ops: every state directory that
should hold state holds it. Post-apply hooks converge and start the
machines — writing each machine's content stamp as they do — the overlay
member last and for the first time (gateway.md §1.1).

**Step 4 — read the failure, if there is one.** Every file's hook asks
systemd whether its machine reached active and fails the resource when it
did not, so a bad push is red rather than silent. What it does **not** do
in this window is put anything back: the swap it reaches for needs the
tree the push displaced, and on a first push there is none — the old tree
went to `machines-old` in step 2, and `machine-rollback` says so and
exits non-zero. A machine that fails to start therefore stays down, and
§6 by hand is the only way back.

## 5. Verification

Run on the device unless noted:

```sh
dig @10.0.5.3 cloudflare.com +short          # alice answers
dig @10.0.5.4 cloudflare.com +short          # bob answers
machinectl list                              # four machines, all running
ls /var/lib/machines                         # four links, and nothing stale
ls /etc/systemd/nspawn                       # four settings files, nothing else
ls /data/on_boot.d    # the four rendered scripts, and 50-authorized-keys.sh until §7
machinectl shell zerotier /usr/sbin/zerotier-cli info
```

`systemctl status udm-boot` says nothing about this push and is not on
the list: the hooks run the boot-chain scripts directly, and `20-units.sh`
never restarts the unit that runs it, so what that status reports is the
previous boot's run of whatever chain was on the device then.

The device carries one piece of older residue — a dangling
`/var/lib/machines/adguard.pre-rename` link and the failed
`systemd-nspawn@adguard.pre-rename.service` beside it. The first push
clears the link: `40-machines.sh` retires every link into the machines
root that the declared set does not name. The failed unit is a runtime
object with no file behind it and goes at the next boot, or to
`systemctl reset-failed`.

-   **The resolvers kept their configuration**: each interface shows the
    filters, clients and rewrites it had before the window. A resolver
    that came up on factory settings means its state did not move.
-   **The proxy serves, and its issuance succeeded.** From a LAN host,
    against the address rather than the name, because the site block ends
    in `handle { abort }` and an unmatched name gets no answer at all:

    ```sh
    curl -sS --resolve unifi.unlimited-code.works:443:10.0.5.180 \
        https://unifi.unlimited-code.works/ -o /dev/null -w '%{http_code}\n'
    ```

    Repeat for each declared vhost and for each legacy vhost the census
    carries. A **new certificate is expected** — the state carried across
    holds the account, not a certificate for these names — so what is
    checked is that issuance finished: the request above serves on a
    certificate for the declared zone, and
    `journalctl -u systemd-nspawn@caddy.service` shows the obtain
    completing rather than retrying.

    **A stalled DNS-01 challenge is the failure to expect here.**
    Neither site block names a resolver, so the propagation check asks
    the challenged zone's own name servers (gateway.md §1), and this
    window is the first time that path runs on this device. The log at
    debug level is what shows it: before each check the proxy writes
    `checking authoritative nameservers` naming the servers it is about
    to ask — which should be the zone's own name servers, never this
    device's resolver — and `certificate obtained successfully` when the
    order completes. What a stall looks like is a propagation timeout
    followed by the proxy's own retries, which are slow and unattended:
    read the log, and do not intervene between them.
-   **A second `pulumi up` reports no changes and restarts nothing**,
    which is the stamp mechanism proving itself on the path every later
    apply takes.
-   **The routing daemon runs this program's configuration.** The
    installed file is the smaller half of that; the daemon is the rest:

    ```sh
    systemctl is-active frr                            # active
    systemctl list-dependencies --reverse frr.service  # frr-config.service
    grep -x 'bgpd=yes' /etc/frr/daemons                # bgpd=yes
    # frr:frr 640 -- as the stock file already is, so this guards the
    # mode rather than proving the push:
    stat -c '%U:%G %a' /etc/frr/frr.conf
    ls /etc/frr/frr.conf.kluster-applied
    cmp /data/custom/frr/frr.conf /etc/frr/frr.conf     # silent
    vtysh -c 'show daemons'                             # lists bgpd
    # "Active" or "Connect" until the worker peers, "Established" after:
    vtysh -c 'show bgp neighbors 192.168.70.10 json' | jq '.[].bgpState'
    ```

    **`systemctl is-enabled frr` is not on that list and never will
    be.** Nothing enables the daemon's unit, so it answers `disabled` on
    a perfectly healthy device; the reverse dependency above is what
    replaces it, because the edge that starts the daemon at every boot
    is a `Wants=` line in `frr-config.service`, and that unit naming
    itself there *is* the edge (§2, gateway.md §1.3).

    **Before the worker VM exists the peer state is `Active` or
    `Connect`** — the daemon holding our configuration and dialing a
    peer that is not there yet. Once the worker peers it is
    `Established`, and `ip route show proto bgp` lists the pool's host
    routes. `cannot start staticd: daemon binary not installed` in the
    journal at every start is expected noise: this firmware's build
    compiles that daemon out, and the set that runs is `watchfrr`,
    `zebra`, `mgmtd` and `bgpd`.

    **What a green run does not prove.** The stamp and the file
    comparison prove the file is installed and `frr.service` came up.
    They do not prove the daemons read it: on this firmware the start
    script brings `watchfrr` up and returns, and the configuration is
    pushed into the daemons afterward by `watchfrr`, whose failure fails
    nothing here. The executable's `vtysh -C -f` pre-check narrows the
    gap — a line this firmware's parser rejects fails the converge
    before anything is installed — but it proves *parsed*, not
    *accepted*. A line can parse and still be refused by the daemon,
    which surfaces only at that later push. The peer state is what
    closes the gap, and is why it is on the list.

    **A retry loop trips the daemon's start limit**, which is three
    starts in three minutes: a first push plus two quick retries fails
    with `start request repeated too quickly`, and the converge stops
    there with the stamp unwritten. `systemctl reset-failed frr` clears
    it, and is what to run before trying again rather than reading the
    red as a fault of the configuration.
-   **Whether a controller pass disturbs the daemon** is the one thing
    about the two managers that reading the device could not settle
    (gateway.md §1.3), and the window opens the answer rather than
    closing it. Read the end state, and write the values down:

    ```sh
    systemctl is-active frr                            # active
    pgrep -x bgpd                                      # record the pid
    cat /proc/"$(pgrep -x bgpd)"/cgroup                # frr.service
    systemctl show -p NRestarts frr                    # record; expect 0
    ip route show proto bgp                            # record
    ```

    **This reading is the *before*, not the answer.** The push does
    provision the gateway for the firewall, but the controller applies
    asynchronously and the daemon's restart happens in the same `up`,
    so neither the streamed order nor these values witness which came
    first — and a pass that killed the daemon would be hidden by the
    restart that followed it, or by the supervisor respawning `bgpd`
    within seconds. One process under `frr.service` is consistent with
    a daemon nothing touched *and* with one that has been killed and
    replaced.

    **The *after* is the ceremony's step 3** (gateway.md §2.5), the
    first controller-side change that happens with this daemon already
    up and the window closed: it declares the inbound pinhole. Re-read
    the block there. The same process id, `NRestarts=0`, still active
    and the route reading unchanged — empty on both sides until the
    worker peers, which is still the check — is what settles it. A
    changed process id, a raised restart count, a `bgpd` in a control
    group other than `frr.service`'s — that one is the controller's own
    — or a `proto bgp` route gone is the single outcome that reopens
    who owns the routing daemon on this device, and it is recorded the
    same way: exactly what was seen, because that is what the question
    would be re-decided on. Nothing is lost either way while no session
    exists, which is what makes this the cheap moment to find out.

    A reboot between the two readings resets both — the process id and
    the restart count belong to that boot. If one falls in between, and
    the soak's first one is meant to (§2), take the *before* again
    after it: the comparison is across the controller pass, not across
    a boot.

**Nothing in §4 or §5 is irreversible.** The point of no return is §7:
the cleanup deletes the old trees, and the removal commit takes the
scripts that converge the old layout out of yadm. Neither happens before
the soak, which runs until the rest of the ceremony has (gateway.md §2.5
steps 2–4) and a `physical` preview over the overlay comes back clean.

## 6. Rollback

Available while `machines-old` is still on the device and yadm still
holds the retiring scripts. On the device:

```sh
for m in adguard-alice adguard-bob caddy zerotier; do
    systemctl disable --now "systemd-nspawn@$m.service"
    rm -f "/var/lib/machines/$m"
done
mv /data/custom/machines/adguard-alice/state /data/adguard-alice
mv /data/custom/machines/adguard-bob/state   /data/adguard-bob
mv /data/custom/machines/caddy/state         /data/caddy/data
mv /data/custom/machines /data/custom/machines-new
mv /data/custom/machines-old /data/custom/machines
```

The four units are the same template instances the old layout used, so
all four are named here. The new machine directory has to be moved aside
before the old one comes back, or the old trees land *inside* it. The
links have to go rather than be left: they point at
`machines/<name>/rootfs`, which stops existing at the rename, and the old
`40-machines.sh` does not repair one. Its test follows the link, sees
nothing at the far end and therefore tries to create it — and `ln -s`
refuses, because the link file itself is there. Every start would fail
against a root filesystem that resolves to nothing.

Then, from the workstation:

```sh
~/.config/gw-config/deploy.sh
ssh gw 'for m in adguard-alice adguard-bob caddy; do
            systemctl enable --now "systemd-nspawn@$m.service"; done'
dig @10.0.5.3 cloudflare.com +short
```

The push restores `on_boot.d/`, `/etc/systemd/nspawn/` and the unit
sources — it mirrors both directories with `--delete`, which is what
takes the new boot chain back off the device — and it starts nothing, so
the machines are enabled by hand.

**And the deployment's own half.** A rollback after step 3 leaves state
recording every gateway resource as applied, so the next `pulumi up`
pushes the new layout straight back into the restored tree. The
device-side children have to leave state before that can happen — each
named individually, each with its own dependents:

```sh
pulumi stack --show-urns | grep 'URN:' | grep -E \
    'kluster-(persistence|nspawn|caddy|adguard-alice|adguard-bob|zerotier|routing|access)$'
# then, for each URN that printed:
pulumi state delete --target-dependents '<urn>'
```

**Not the gateway component itself.** Deleting the parent takes every
descendant with it, and one of them is `kluster-firewall`, whose
resources live on the *controller* — the cluster VLAN, its firewall zone,
the address groups and the zone policies. The device-side rollback does
not touch any of those, so they still exist; forgetting them from state
leaves the next apply trying to create a second VLAN 7 with duplicate
policies, or failing on the names. `kluster-firewall` stays in state
because nothing undid what it did.

**`pulumi destroy` is not a way back**, with or without a target. A unit
resource's delete disables the unit and removes the live copy, and
`udm-boot.service` is one of this program's units: destroying it leaves
the boot chain's own unit disabled, which the retiring push restores as a
file and never enables. The paths destroy would take are the ones the
rollback just put back — the boot-chain scripts and the unit sources —
so run after §6 it undoes the rollback rather than the push.

Nothing runs an apply unattended in the meantime — continuous integration
cannot reach the LAN (§3) — so the window between the device-side
rollback and the state edit is not a race.

**What the rollback leaves behind, and why it is inert.** The device is
serving again with all of it in place:

-   `frr-config.service` and `authorized-keys.service`, enabled, with
    their executables under `bin/`, and `machine-rollback` beside them —
    the retiring push writes one file into `bin/` and takes nothing out
    of it. The keys one is append-only. The routing one is not idle: it
    wants `frr.service`, so it starts the daemon again at every boot,
    and its executable holds the toggle and the installed configuration
    where they are.
-   **`frr.service`, running** — `watchfrr`, `zebra`, `mgmtd` and
    `bgpd`, and no `staticd`, which this firmware's build compiles out.
    The push started it and nothing in §6 stops it. Going back to what
    the device ran before the window is therefore two commands rather
    than one: `systemctl disable --now frr-config.service` takes the
    boot edge away, and `systemctl stop frr` stops the daemon. `bgpd=yes`
    stays in `/etc/frr/daemons` — one line in a file a firmware update
    restores anyway, and inert with the daemon stopped.
-   `/etc/frr/frr.conf` on the declared content, with its stamp beside
    it. The stock file is not restored, and nothing routes differently
    for it: the peer it declares is the worker VM, which does not exist,
    so the daemon dials an address that never answers and learns no
    route (§2).
-   `/data/custom/frr`, one directory holding one configuration file.
-   `machines-new/`, which is where the overlay member's minted identity
    now lives — `machines-new/zerotier/state`. **A retry of the window
    has to carry that directory across**, or the member mints a second
    identity and the node id read in the ceremony's next step is not the
    one the roster was about to authorize.

## 7. Cleanup and retirement

Every absorbed resource ends with a removal commit in its old tracker
(cluster/migration.md rule 0.3). These land after the soak, each in its
own repository, and they are what ends §6.

**On the device**, delete what nothing declares: `/data/custom/machines-old`
(the old trees and their `.old` copies), `/data/adguard-*` if anything is
left of them, the rest of `/data/caddy`, `/data/custom/nspawn/`, and
`/data/on_boot.d/50-authorized-keys.sh`, whose work is now a unit and an
executable (gateway.md §1.4).

**gw-config (yadm) — the whole repository retires.** What it holds and
what holds it now:

| Retiring | Now |
| --- | --- |
| `on_boot.d/10-packages.sh`, `20-units.sh` | rendered by the persistence layer |
| `on_boot.d/30-nspawn-units.sh`, `40-machines.sh` | rendered by the nspawn runtime |
| `on_boot.d/50-authorized-keys.sh` | a unit and an executable in `bin/` |
| `units/udm-boot.service` | vendored template, upstream pin in its header |
| `units/nspawn-bridge-watchdog.service`, `bin/nspawn-bridge-watchdog.sh` | the runtime's unit and executable |
| `nspawn/*.nspawn` | one settings file per machine, rendered |
| `authorized_keys.d/kluster-physical.pub` | constructor data of the keys component |
| `caddy/Caddyfile`, `caddy/resolv.conf` | two rendered files the machine mounts |
| `secrets/cf_token` | the minted token, delivered as a device secret |
| `deploy.sh` and the daily drift check | `pulumi preview`, which diffs the device itself |
| `backups/adguard-*` | the state directory, which is now the persisted thing; the rewrites in it are the `dns` stack's declaration |
| `backups/machines-manifest.txt` | the declaration: pins, machine set and settings are all in the program |

**The UniFi autobackup relocates rather than retires.** It is the
recovery path for everything the controller holds that no declaration
covers — device adoption, system settings, admin accounts — so it
outlives the repository that happened to store it. Its new home is
`~/.config/gw-backups`, yadm-managed, holding autobackups alone, which
lets the gw-config directory be deleted whole. Four edits make that true:

-   **The pull job** keeps only its UniFi transfer, with
    `~/.config/gw-backups` as the destination. The resolver-snapshot and
    machine-manifest transfers go with the rows above — and the manifest
    reads paths that no longer exist.
-   **The daily check** loses its drift section, which a preview answers
    now, and keeps what the device still needs: the four machines
    running, the boot chain enabled with its last run clean, the offline
    package cache matching the rendered set — `systemd-container`,
    `libnss-mymachines`, `skopeo`, `umoci`, with `rsync` gone from it —
    and a certificate probe, which now names a declared vhost and reaches
    it with `curl --resolve` rather than through a `lan.ucw.phd` name.
-   **The two timer units** (yadm `##h` alternates on the homelab host)
    keep their schedules and lose gw-config from their descriptions; both
    are re-enabled once the scripts above are.
-   **The homelab-ops pin** in yadm's mise configuration moves to the
    commit carrying those scripts, which is what puts them on the host.

**homelab-containers** keeps building and publishing the images and stops
pushing them: a root filesystem is delivered as a digest-pinned artifact
the device pulls itself, so the repository's device-push recipe retires
with a pointer to that, and the `.old` rollback copies it left beside
each tree go with it.

**This document retires with the window.** It describes a move that
happens once; the layout it moves to is described where the device is
(gateway.md §1), so the change that marks rule 0.3's gw-config row done
deletes this file as well.
