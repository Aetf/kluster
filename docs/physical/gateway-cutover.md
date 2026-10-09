# Gateway Cutover

The one maintenance window that hands the device over: it stops being
converged by the retiring gw-config repository and starts being converged
by this program. It is the opening of the bring-up ceremony
([gateway.md](gateway.md) §2.5) — the first push from this program is
the push this window prepares for, and what has to happen before it is
moving the live container state to where the declaration expects it.
§8 carries the steps of the first milestone's bring-up that follow
the ceremony and are not the gateway's: the bring-up verifications
that need no network plugin, the `dns` stack's first `up`, CI's overlay
identities and the zones' DS records.

Everything below runs as `root` in a LAN session on the device, except
where a step says otherwise. That is not a preference: there is no
overlay member yet — the member is one of the things this push delivers —
which is why the ceremony's first three steps dial the LAN.

The steps that run `pulumi` against `physical` run on the operator's
workstation instead, through one shell function. Define it once, from
the root of the checkout that holds `.credentials/`, in the shell those
steps use:

```sh
CHECKOUT=$PWD
physical() {
    (cd "$CHECKOUT" && mise x -- env -u PULUMI_CONFIG_PASSPHRASE \
        PULUMI_CONFIG_PASSPHRASE_FILE=.credentials/physical.passphrase \
        pulumi "$@" --stack physical)
}
```

`physical` is encrypted under a passphrase of its own, and `mise.toml`
hands every run the stack passphrase, so the function takes that one
out of the run's environment and names `physical`'s slot instead
(credentials.md §4.4). A bare `pulumi` against `physical` ends in
`error: incorrect passphrase`. The stack is under its own passphrase
before this window opens (§3).

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
    path is the machine's own directory, holding `rootfs/`, its digest
    marker, `state/`, `initial-state/` where the machine has one, the
    `<name>.nspawn` settings, the content stamp, and the files the
    machine mounts (gateway.md §1.1). What makes it a machine to the
    boot chain is the settings file: nothing on the device is handed a
    list. The whole of today's `machines/` is moved aside in
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
    executable's stamp lives beside the installed file in `/etc` and
    names the firmware release. A reboot on the same release finds file
    and stamp equal and exits having done nothing, with the daemon up
    regardless because the edge started it. The first boot on a new
    release parses and restarts once (gateway.md §1.3). And **no session
    comes up until the worker VM exists** to peer with: the declared
    peer is dialed and nothing answers, which is the expected state
    rather than a fault.

    **The edge itself is proven only after a reboot**, which the push
    cannot show and which nothing here schedules — the device takes one
    on its own whenever an unattended firmware pass lands. So §5's
    routing block is run a second time once the soak has taken a
    reboot: `active` then is the edge working, `inactive` then is the
    edge failed. That is the one reading a healthy device cannot give,
    and the only thing about the boot path the push itself cannot show.
    The edge alone is read on a boot whose `frr-config.sh` did nothing:
    `journalctl -b -u frr-config.service` shows no `frr-config: checking`
    line. The first boot after a firmware pass is not such a boot.

## 3. Before the window opens

-   **`physical` is under its own passphrase.** `credentials derived
    physical-passphrase re-encrypt` has moved it and `credentials
    derived sync --only physical-passphrase` has pushed the passphrase
    into its Environments (credentials.md §4). The move comes before
    `physical`'s first `up`, which step 3 is: the state backend keeps
    every checkpoint written before a move, readable under the stack
    passphrase (credentials.md §1 rule 6). The `physical()` function
    above opens the stack under that passphrase alone.
-   **The legacy vhost census has landed.** The declared `Caddyfile`
    serves the controller console and the two resolver interfaces; the
    live one serves eleven names under `lan.ucw.phd` whose apps migrate
    in Waves B–D (cluster/migration.md §2). Those rows are carried into
    the declaration, so the cutover is not a user-visible regression
    (Aetf/kluster-ops#155, ruled; #163 builds it). Without them on the
    branch being applied, the window takes those names down for weeks.
-   **The package set is installed already.** The push's first act is
    `10-packages.sh`, which exits without doing anything when
    `systemd-container`, `libnss-mymachines`, `skopeo`, `umoci` and
    `rsync` are all present, and fails the push when apt cannot reach
    the internet and the offline cache cannot satisfy them — *after* the
    machines have been stopped and moved. Installing them in advance
    takes that failure out of the window: `apt-get install -y
    systemd-container libnss-mymachines skopeo umoci rsync`, on any day
    before it. `rsync` is there today, from gw-config's own package
    script, and stays in the set for the backup pull (§7). The pull
    probe below needs `skopeo` and `umoci` in place.
-   **The ACME token is minted and committed**: `credentials derived
    cloudflare-gateway-acme mint` has run and the `physical` stack file
    carrying the token is committed (credentials.md §3). The proxy comes
    up with no way to renew otherwise.
-   **`gatewayBootstrapHost` is set to a literal LAN address of the
    device — never a name — and committed** (gateway.md §2.5). This
    commit makes CI's `physical`
    jobs fail until the ceremony's last step unsets it again: continuous
    integration reaches the site over the overlay and has no route to the
    LAN, and an unreachable device fails the whole `physical` preview by
    design (gateway.md §3). Nothing applies the stack unattended in the
    meantime; the operator's session is the only one that can.
-   **The bootstrap address answers, and it is an address.** A name
    would be unresolvable at the only moment the key is read: §4 stops
    everything on the LAN that answers for one. Confirm the value the
    push will dial, from the workstation, on any day before:

    ```sh
    ADDR=<the committed gatewayBootstrapHost>
    ssh "root@$ADDR" true                                       # the shell door
    curl -skS -o /dev/null -w '%{http_code}\n' "https://$ADDR/"  # the controller door
    ```

    The value is bound once and used twice on purpose: the knob takes
    whichever of the device's legs the workstation reaches
    (gateway.md §2.5), so a check that spells an address of its own
    can pass on a box the window will never dial. Both doors are the
    same device behind two ports, so what the window needs is one
    address that answers on both — any status code from the second,
    since what is being tested is that something terminates there.
    The two probes below dial the same `ADDR`. They need the value
    `ADDR` will hold, not its commit, and are best run before it.
-   **The device pulls a pinned image the way the push does.** Each
    machine's root filesystem in step 3 is two commands on the device,
    and the window is otherwise the first time either runs there: the
    provider's `pull_script` and `unpack_script`
    (`providers/device_files/provider.py`) spell their flags from the
    manual pages of the releases the device's distribution ships —
    `skopeo` 1.2.2, `umoci` 0.4.7 — and the copy leans on
    `/etc/containers/policy.json` without naming it. The probe runs
    both invocations as the provider builds them, against one of the
    gateway's pins, into a scratch path under `/data`, over the same kind of
    session the provider opens — a command handed to `ssh`, with no
    terminal. From the checkout root, in the session that bound `ADDR`,
    with the packages above installed:

    ```sh
    IMAGE=$(sed -n 's/^ *versions:image-gateway-zerotier: \(.*\):[^:@/]*\(@sha256:[0-9a-f]*\)$/\1\2/p' Pulumi.yaml)
    echo "$IMAGE"       # <repository>@sha256:<digest>, tag dropped as the provider drops it;
                        # an empty line means the pin did not parse
    T=/data/kluster-probe/rootfs
    ssh "root@$ADDR" cat /etc/containers/policy.json
    time ssh "root@$ADDR" "mkdir -p $T.kluster-oci && skopeo copy --quiet docker://$IMAGE oci:$T.kluster-oci:pinned"
    ssh "root@$ADDR" "umoci raw unpack --image $T.kluster-oci:pinned $T.kluster-unpacking && ls $T.kluster-unpacking"
    ssh "root@$ADDR" rm -rf /data/kluster-probe
    ```

    It passes on three readings. The policy file names a `default` of
    `insecureAcceptAnything`, the permissive default that `skopeo`'s
    dependency `golang-github-containers-common` ships. The copy exits
    zero, prints nothing, and takes seconds. Step 3 pulls the proxy's
    and both resolvers' images at the same time over this uplink —
    nothing orders the three trees against each other — and holds each
    pull to one command under the session's `DEFAULT_TIMEOUT`
    (`providers/device_files/ssh.py`); their images together are several
    times this one, so a copy here that takes a noticeable fraction of
    that bound is step 3's pulls running out of it.
    And the unpack exits zero with `ls` listing a root filesystem's top
    level — `etc`, `usr` and the rest — rather than a `config.json`
    beside a `rootfs`, which is the runtime bundle that `umoci unpack`
    writes and the provider's `raw` subcommand exists to avoid.

    Each failure has one meaning. `command not found` is the packages
    item above not yet run. A usage error from either tool — a flag, a
    subcommand or a reference shape the release does not know — is the
    provider's command shape not fitting the device, and it is fixed in
    `pull_script` or `unpack_script` before the window; nothing else in
    the design moves. A copy refused by its signature policy, or failing
    because the policy file is missing, is the one input to the copy
    that the provider never names: it passes neither `--policy` nor
    `--insecure-policy`, so the file or the provider changes before the
    window. A copy that cannot reach the registry is
    the device's own resolution or outbound HTTPS failing with its
    resolvers **up**, which the window, with them down, cannot improve
    on. And a copy that takes minutes is step 3's concurrent pulls
    exceeding that bound, after the state has already moved.

    The last line is the cleanup, and all of it: the path is the
    probe's own, and no machine, unit or declaration names it. Between
    the unpack and that line the image sits on the device in two forms,
    which is the same peak each of step 3's pulls reaches.
-   **The controller round-trips the firewall's resource shapes.**
    Step 3 is otherwise the first time the bridged filipowm/unifi
    provider writes to this controller: the targeted apply creates the
    cluster zone and the zone policies and their orders (gateway.md
    §4.2). It creates no port forward: the census's one forward waits
    for qbittorrent to move onto the worker, and the legacy host's
    hold on that port is untouched by the window. The zone-policy
    resource is marked experimental upstream, and whether it
    round-trips — create, a clean
    preview, delete — decides which provider declares the rules
    (declarative/physical.md §6). A window that finds out has already
    moved the machines' state, so the answer comes from scratch objects
    beforehand.

    The probe is a throwaway Pulumi project with a file backend of its
    own, in the form framework/testing.md §5.1 gives a scratch probe,
    run in the stack's own `.venv` so that the SDK — and with it the
    provider release the engine resolves — is the one the stack pins.
    Its provider is built by `controller_provider`
    (`components/gateway/unifi.py`), the function `SiteFirewall` builds
    its own with, from the same key in the `physical` stack's
    configuration, at the same `ADDR`. That includes the connection's
    posture: the controller's certificate is not verified, because it
    names none of the addresses the program dials and the provider can
    pin nothing, and what that exposes is recorded in architecture.md
    §4.1. It declares a zone of its own
    with no network in it; two address groups of each family, one to
    source from and one to send to, as the census's IoT and pool groups
    are; policies out of that zone in the shapes the census uses — zone
    to zone in both families, and per family a subnet source, named
    through its group, to a literal address with a port and to a group;
    the order on that pair; and a forward of an unused WAN
    port to the worker's address, each named `kluster-probe` in the
    console. The addresses are the documentation ranges, `192.0.2.0/24`
    and `2001:db8::/32`. A zone holding no network sources no traffic,
    so nothing its policies say matches anything; the forward is open for the
    minutes the probe takes, to an address nothing answers on before
    the worker exists. The cluster VLAN's network object is not probed:
    a scratch network is a live VLAN on the gateway rather than an inert
    object, so that creation stays the window's.

    Confirm in the console that no port forward uses 49999; if one
    does, pick another unused port for `PORT` below. Then, from the
    checkout root, in the session that bound `ADDR`:

    ```sh
    PROBE=$PWD/.claude/unifi-probe
    PULUMI=$(mise which pulumi)
    probe() {
        (cd "$PROBE" && PULUMI_BACKEND_URL="file://$PROBE/state" PULUMI_HOME="$PROBE/home" \
            PULUMI_CONFIG_PASSPHRASE=probe "$PULUMI" "$@")
    }
    mise x uv -- uv sync
    mkdir -p "$PROBE/state"
    cat > "$PROBE/Pulumi.yaml" <<'EOF'
    name: unifi-probe
    runtime:
      name: python
      options:
        toolchain: uv
        virtualenv: ../../.venv
    EOF
    cat > "$PROBE/__main__.py" <<'EOF'
    import pulumi
    import pulumi_unifi as unifi

    from kluster import conventions
    from kluster.components.gateway import url_host
    from kluster.components.gateway.unifi import ZONE_EXTERNAL, controller_provider

    NAME = 'kluster-probe'
    PORT = '49999'  # a WAN port no forward on the controller uses
    site = conventions.gateway.UNIFI_SITE
    config = pulumi.Config()

    # The construction SiteFirewall builds its own provider with.
    provider = controller_provider(
        f'{NAME}-unifi', api_url=f'https://{url_host(config.require("gatewayHost"))}', site=site
    )
    opts = pulumi.ResourceOptions(provider=provider)
    external = unifi.get_firewall_zone_output(
        name=ZONE_EXTERNAL, site=site, opts=pulumi.InvokeOptions(provider=provider)
    ).id

    zone = unifi.FirewallZone(f'{NAME}-zone', name=NAME, site=site, opts=opts)
    group_v4 = unifi.FirewallGroup(
        f'{NAME}-group-v4', name=f'{NAME} v4', type='address-group', members=['192.0.2.0/24'],
        site=site, opts=opts,
    )
    group_v6 = unifi.FirewallGroup(
        f'{NAME}-group-v6', name=f'{NAME} v6', type='ipv6-address-group', members=['2001:db8::/48'],
        site=site, opts=opts,
    )
    # A zone policy's `ips` takes single addresses, so a subnet source is a group too.
    source_v4 = unifi.FirewallGroup(
        f'{NAME}-source-v4', name=f'{NAME} source v4', type='address-group', members=['192.0.2.0/25'],
        site=site, opts=opts,
    )
    source_v6 = unifi.FirewallGroup(
        f'{NAME}-source-v6', name=f'{NAME} source v6', type='ipv6-address-group',
        members=['2001:db8:1::/64'], site=site, opts=opts,
    )
    Source = unifi.FirewallZonePolicySourceArgs
    Destination = unifi.FirewallZonePolicyDestinationArgs
    wide = unifi.FirewallZonePolicy(
        f'{NAME}-wide', name=f'{NAME} wide', description=NAME, action='ALLOW', ip_version='BOTH',
        protocol='all', source=Source(zone_id=zone.id), destination=Destination(zone_id=external),
        auto_allow_return_traffic=True, enabled=True, opts=opts,
    )
    literal_v4 = unifi.FirewallZonePolicy(
        f'{NAME}-literal-v4', name=f'{NAME} literal v4', description=NAME, action='ALLOW',
        ip_version='IPV4', protocol='tcp', source=Source(zone_id=zone.id, ip_group_id=source_v4.id),
        destination=Destination(zone_id=external, ips=['192.0.2.200'], port=443),
        auto_allow_return_traffic=True, enabled=True, opts=opts,
    )
    literal_v6 = unifi.FirewallZonePolicy(
        f'{NAME}-literal-v6', name=f'{NAME} literal v6', description=NAME, action='ALLOW',
        ip_version='IPV6', protocol='tcp', source=Source(zone_id=zone.id, ip_group_id=source_v6.id),
        destination=Destination(zone_id=external, ips=['2001:db8:2::1'], port=443),
        auto_allow_return_traffic=True, enabled=True, opts=opts,
    )
    grouped_v4 = unifi.FirewallZonePolicy(
        f'{NAME}-grouped-v4', name=f'{NAME} grouped v4', description=NAME, action='BLOCK',
        ip_version='IPV4', protocol='all', source=Source(zone_id=zone.id, ip_group_id=source_v4.id),
        destination=Destination(zone_id=external, ip_group_id=group_v4.id), enabled=True, opts=opts,
    )
    grouped_v6 = unifi.FirewallZonePolicy(
        f'{NAME}-grouped-v6', name=f'{NAME} grouped v6', description=NAME, action='BLOCK',
        ip_version='IPV6', protocol='all', source=Source(zone_id=zone.id, ip_group_id=source_v6.id),
        destination=Destination(zone_id=external, ip_group_id=group_v6.id), enabled=True, opts=opts,
    )
    unifi.FirewallZonePolicyOrder(
        f'{NAME}-order', source_zone_id=zone.id, destination_zone_id=external,
        before_predefined_ids=[p.id for p in (wide, literal_v4, literal_v6, grouped_v4, grouped_v6)],
        site=site, opts=opts,
    )
    unifi.PortForward(
        f'{NAME}-forward', name=NAME, port_forward_interface='wan', protocol='tcp_udp', src_ip='any',
        dst_port=PORT, fwd_ip=str(conventions.HOMELAB_NODE_IPV4), fwd_port=PORT, site=site, opts=opts,
    )
    EOF
    probe stack ls --all       # an empty table: the backend in hand is the probe's own
    probe stack init probe
    probe config set gatewayHost "$ADDR"
    physical config get unifiApiKey | probe config set --secret unifiApiKey
    ```

    The key is read out of `physical`'s configuration through the
    `physical()` function defined at the top of this runbook, under that
    stack's own passphrase.

    The rest runs one line at a time, because each reads the result of
    the one before it — above all, `rm -rf` runs only once `destroy`
    has succeeded or its leftovers are gone by hand (below):

    ```sh
    probe up                   # read the plan before confirming it
    probe preview --refresh --expect-no-changes
    probe destroy --remove
    rm -rf "$PROBE"
    ```

    `probe` is the scratch form rather than a convenience: the binary
    `mise which` names, with the backend, the plugin home and a
    passphrase of the probe's own set in the same command, so no run of
    it can land in the live backend. Its first run downloads, into
    that home, the plugins the SDKs in `.venv` name.

    It passes on three readings. `probe up` plans the stack, its
    provider and one create for each resource the program declares —
    the zone, the groups, the policies, the order and the forward — and
    nothing else, and applies every one. The refreshed preview exits
    zero proposing no change: every field the controller reads back is
    the value declared. And `destroy` deletes every one of them, after
    which the console holds nothing named `kluster-probe`.

    What a failure means depends on where it lands:

    -   **401 or 403, on the lookup of `External` or on the first
        create.** The key, not the resources: it is not the dedicated
        administrator's, or that administrator cannot manage the
        network; the fallback would be refused the same way. The key is
        re-recorded (`credentials derived unifi record`, credentials.md
        §3) and the probe runs again.
    -   **The zone, a group, a policy or the order refused, or a
        delete refused.** The resources the rules are made of do not
        round-trip on this controller's release — the case
        architecture.md §5.1 records a fallback for: a
        `UnifiFirewallPolicy` resource on the device-files provider,
        driving the controller's API directly. That resource is not
        built. The rules move to it before the window, and the window
        waits on it.
    -   **The refreshed preview proposes a change.** The diff names the
        field. A field the controller reads back as another spelling of
        the same value is answered in the component, by declaring that
        spelling, and the probe runs again; a field whose value the
        controller does not keep is the round trip failing, as above.
    -   **The forward refused.** A refusal of the request itself is the
        port-forward endpoint not taking writes on this release, and
        the fallback above is for zone policies only: nothing in the
        design answers it, so it is filed for a ruling before Wave D,
        the first apply that writes the forward — the window's run
        declares none. A refusal that names the destination as outside
        every network is the controller wanting the cluster VLAN in
        place first. The forward's first apply comes waves after the
        window has made the network, so this one blocks nothing; it is
        still filed, because the component gives the forward no
        dependency on the network, and an apply that declares both at
        once would meet it. A refusal naming a conflict with an
        existing forward is the probe's port choice, not the endpoint.
    -   **The program fails before any row is planned.** That is the
        probe's own setup — the `.venv`, the configuration — and
        says nothing about the controller.

    Cleanup after a failure is the same `probe destroy --remove` and
    `rm -rf`; whatever `destroy` cannot delete is removed in the console
    by its `kluster-probe` name before the directory goes. The directory
    goes in every case: the probe's configuration and its state history
    hold the controller's key under a passphrase anyone can read above.
-   **The workstation running step 3 resolves through something other
    than alice and bob.** Both are down for the whole of §4, and the
    run dials by name: the stack program reads the account's
    availability and fault domains off the cloud API before it
    declares the gateway, targeted or not; step 3's targeted run reads
    the overlay network off ZeroTier Central for its adoption
    (gateway.md §2.5 step 1); and step 5 reaches Central again,
    Backblaze and the Talos image factory as well. What the
    run does *not* need a resolver for is the short list of endpoints
    this program deliberately spells as addresses — the state backend
    (state-backend.md), the libvirt session at the homelab host's
    overlay address, and the device itself at `gatewayBootstrapHost`.
    Confirm before starting:

    ```sh
    resolvectl dns    # no link may name 10.0.5.3, 10.0.5.4,
                      # fd1a:665f:8bcb:5::a00:503 or fd1a:665f:8bcb:5::a00:504
    ```

    `resolvectl dns` rather than `resolvectl status`: the latter is
    hundreds of lines on a workstation with many links, its `Global:`
    block names an upstream that says nothing about the per-link
    servers below it, and the LAN's pair can sit a hundred lines down
    where nobody reading the top of the output will see it. Both
    families are listed because the leases hand the pair out over
    IPv6 as well, and two IPv4 literals do not exclude those.

    Then the reading that does not depend on knowing every way a
    resolver can be configured: with both machines stopped, a lookup
    from this workstation still answers. It is taken inside the
    stopped span the next check opens, which is why that span covers
    the device and the workstation together rather than each opening
    one of its own. **The cache is flushed first**, and that is not
    hygiene: where `/etc/resolv.conf` names a local stub at
    `127.0.0.53`, a name already in the stub's cache is answered from
    it regardless of whether the upstream behind it is reachable — so
    the unflushed reading goes green on exactly the workstation this
    check exists to catch. A workstation that took its resolver from
    the LAN's own DHCP fails the flushed one, and fails every lookup
    of the window from step 1 onward with no resolver left to fall
    back on. Which workstation step 3 runs from is therefore a choice
    made before the window, not during it.
-   **The device still resolves with its resolvers stopped.** The
    root filesystems `skopeo` pulls in step 3 resolve the way every
    ordinary program on the device does — through whatever
    `/etc/resolv.conf` names, and through the device's own forwarder
    only if that is what it names. Two readings settle it, and the
    second shares its stopped span with the workstation's above — a
    sequence of pastes rather than one script, because the device and
    the workstation have to be read while the same pair of containers
    is down. **That span is the LAN's own DNS, stopped outside the
    window**, so it lasts the seconds the readings take and not the
    time it takes to find the other terminal: have both sessions open
    before the first paste.

    ```sh
    # on the device, first and on its own
    cat /etc/resolv.conf    # nameserver 127.0.0.1, and no other nameserver line

    # on the device: open the span
    for m in adguard-alice adguard-bob; do
        systemctl stop "systemd-nspawn@$m.service"
    done

    # on the workstation, while they are down
    resolvectl flush-caches && dig ghcr.io +short

    # on the device, while they are down
    COLD=ghcr.io    # or any public name, if this check has run before
    getent hosts "$COLD"

    # on the device, once both readings are in
    for m in adguard-alice adguard-bob; do
        systemctl start "systemd-nspawn@$m.service"
    done
    ```

    `getent hosts` rather than a dig at a chosen server: a dig that
    names `127.0.0.1` proves the forwarder answers, which is not the
    question — a `resolv.conf` naming one of the machines would leave
    that dig green and every pull in the window red. The device caches
    answers too, so a name it has been asked for before can come back
    from that cache while the upstream behind it is the pair that is
    down. **The way past that is a name it has never been asked for.**
    No cache can hold one, so nothing about what the device's cache
    keeps, or for how long, has to be known to trust the reading.
    `ghcr.io` is the faithful choice on a first run, because it is the
    name the pulls themselves resolve; it stops being cold the moment
    this check has used it, so a repeat run picks some other public name
    — one under no domain this site answers for itself. That condition
    is what keeps a substitute as good as the original: a cold name
    outside the site's own domains can be answered only by a live
    upstream, and `getent` reaches it by the path the pulls take.

    An answer to a name the device has not been asked for before means
    §4's pulls proceed with the LAN dark. No answer means the path
    those tools take ends at the resolver pair, and the window does
    not open until that is changed: the pulls would fail inside it,
    after the state has already moved.
-   **The window's targeted apply has been previewed.** Step 3 runs
    `pulumi up` against the gateway's resources alone, named by URN.
    Those URNs are derived from the component declarations
    (`stacks/physical.py`, `components/gateway/`) rather than read off
    an applied stack, because nothing has ever applied this one — so
    they are confirmed by a preview, which costs nothing and needs no
    window:

    ```sh
    physical preview \
        -t 'urn:pulumi:physical::kluster-py::kluster:gateway:Gateway::kluster' \
        -t 'urn:pulumi:physical::kluster-py::kluster:gateway:Gateway$**::**'
    ```

    What it must show is the gateway's own resources and nothing else:
    the persistence layer, the nspawn runtime, the four machines, the
    routing session, the authorized key and the controller-side
    firewall — no cloud instance, no overlay member, no bucket, no
    worker VM. One row that is not the gateway's is expected beside
    them: a single `=` on `zerotier:index/network:Network
    kluster-network`, the overlay network read into state at Central's
    current values, because the engine performs an import before it
    asks whether a resource is targeted (gateway.md §2.5 step 1). That
    row changes nothing at Central. A preview without it means the
    adoption rides the run with no targets instead; the update to the
    network is in that run either way, and is read there before it is
    applied. A preview that matches nothing, or that plans the whole
    stack, means the targets do not select what they were derived
    from; the window then runs `pulumi up` with no targets at all and
    takes the risk gateway.md §2.5 sets out.
-   **The host-side timers are stopped**: `check-gw.timer`,
    `gw-backup.timer` and `autodeploy-containers.timer` — see step 0,
    which is inside the window only because it must not be forgotten.
-   **The window's time limit is written down: `<LIMIT: set by the
    operator before the window opens>`**, counted from step 1's first
    stop. From that moment the LAN has no resolver, so the limit is how
    long the household goes without DNS while a failure is read. When it
    passes with alice and bob not answering §5's first two readings, the
    window stops diagnosing and runs §6 (§4 step 4).
-   **No `pulumi` run of the bring-up overlaps the state backend's
    update window**: Tuesdays, 04:00 to 05:00 UTC (`REBOOT_DAY`,
    `REBOOT_TIME` and `REBOOT_WINDOW_MINUTES` in
    `lib/state_backend/settings.py`, rendered into the Butane file with
    no time zone, so Zincati reads them as UTC, its default). Zincati
    reboots the box into each new Fedora CoreOS release at the window
    after it is published, and Postgres goes down with it. Every run
    that reads or writes state holds the backend for its whole length:
    step 3, step 5, the ceremony's applies, and §8's `dns` first `up`.
    How an update in flight fares when the backend's server restarts
    is untested here, so the runs are scheduled around the
    window rather than through it: none starts that could still be
    running at 04:00 UTC on a Tuesday, and none starts inside the
    hour. In US Pacific time that is a
    Monday evening, 21:00 under daylight time and 20:00 outside it.
    Once CI holds its identities (§8.3), a merge starts a deploy chain
    that holds the backend too, so the same hour is one to merge
    nothing in.
-   **A current UniFi autobackup is in hand** (§7): unrelated to the
    machines, and the cheapest insurance in the window.

## 4. The window

Downtime is the whole of steps 1 to 3, not the renames alone: the moves
are instant, but the push that follows pulls four root filesystems from
the registry over the site's uplink and unpacks each one, which is tens
of minutes rather than minutes. **Step 3 is also the first apply the
`physical` stack has ever had**, which is why it is run against the
gateway's resources alone rather than as a plain `pulumi up`; what the
form with no targets would put inside this window, and what it costs to
leave out, is gateway.md §2.5.

The LAN has no resolver for that entire span — every lease names alice
and bob, and both are down. The device's own resolution is expected to
survive that, so the pulls proceed regardless — §3's reading of
`/etc/resolv.conf`, with `getent hosts` taken while both machines are
stopped, is what establishes it, and it is a precondition rather than a
remark because a device that resolves through the machines it hosts
fails every pull in this window.

**Step 0 — stop the host-side timers**, on the homelab host, as the user
that owns them:

```sh
systemctl --user disable --now check-gw.timer gw-backup.timer autodeploy-containers.timer
systemctl --user is-enabled check-gw.timer gw-backup.timer autodeploy-containers.timer  # disabled, three times
systemctl --user is-active check-gw.timer gw-backup.timer autodeploy-containers.timer   # inactive, three times
systemctl --user is-active autodeploy-containers.service                                # inactive
```

Stopping a timer leaves a run it already started alone, and a deploy
the reconciler is in the middle of would land its stop and its renames
among step 1's and step 2's. So when the last line prints `activating`,
step 1 waits until the same line prints `inactive`; it can also print
`failed`, from a run that has ended, and only `activating` is waited
out. The run holds a lock
and ends on its own; killing it partway through its `ssh` to the device
is worse than the wait.

A window called off after this step and before step 1 has changed
nothing on the device, and the way back from it is §6's last block
alone, which brings the timers back.

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

The third timer is the other writer of the machines' trees. Every five
minutes it runs the homelab-ops script `bin/autodeploy-containers`,
which fetches the `main` branch of homelab-containers and, for each of
`adguard-bob`, `caddy` and `adguard-alice` whose paths changed since
its last run, deploys that
target to the device with the repository's `just deploy` recipe. The
recipes assume the layout step 2 moves away from. The proxy's
validates the new binary against the residue under `/data/caddy`, which
is still there until §7, then stops the machine, moves
`/data/custom/machines/caddy` — the declared machine's directory, its
state and the ACME account included — to `caddy.old`, and starts a
bare tree in its place. The declared settings bind a state directory
that is no longer where they name it, so the machine fails to start
and retries every five seconds, every LAN vhost is down, and with the
daily check off the reconciler's own failure mail is the only notice. A
merge that touches only `caddy/`
takes that path. The resolvers' recipe fails safe at its first copy,
because their old state directory has moved, and that failure stops
every target after it. So the reconciler stops with the other two, and
§7 retires it rather than bringing it back.

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

**Step 3 — apply the gateway's resources** from the operator's
workstation, over the LAN:

```sh
physical up \
    -t 'urn:pulumi:physical::kluster-py::kluster:gateway:Gateway::kluster' \
    -t 'urn:pulumi:physical::kluster-py::kluster:gateway:Gateway$**::**'
```

It delivers the boot chain, the unit sources, the executables, the
routing configuration, the authorized key, and for each machine its
settings file, its root filesystem, its mounted configuration and
secrets, its initial state where it has one, and the digest marker
naming the pin its tree came from. Installing the initial states is a
no-op: every state directory that should hold state holds it.
Post-apply hooks converge and start the machines — writing
each machine's content stamp as they do — the overlay member last and
for the first time (gateway.md §1.1).

**The targets are what keeps the rest of the stack's first apply out
of this window.** Why that is safe, what the run without them adds and
what targeting does not remove are one argument, and it sits with the
ceremony it belongs to: gateway.md §2.5. What this step needs from it
is that a red `pulumi up` says the update failed rather than which
half of it converged, and that everything outside the gateway is
applied at step 5 instead, with the LAN's DNS back up.

**Step 4 — read the failure, if there is one.** Every file's hook asks
systemd whether its machine reached active and fails the resource when it
did not, so a bad push is red rather than silent. What it does **not** do
in this window is put anything back: the swap it reaches for needs the
tree the push displaced, and on a first push there is none — the old tree
went to `machines-old` in step 2, and `machine-rollback` says so and
exits non-zero. A machine that fails to start therefore retries every
five seconds instead of settling in `failed`, and §6 by hand is the only
way back.

**The reading has the time limit §3 wrote down**, counted from step 1:
`<LIMIT: set by the operator before the window opens>`. Once it has
passed with alice and bob not answering §5's first two readings, the
window stops diagnosing, takes down whatever it learned, and runs §6.
A step 3 still running at the limit is let return first rather than
interrupted, because a cancelled `up` can leave its operations pending
in state, which §6's state edit would then have to work around.

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
root whose machine has no settings file behind it. The failed unit is a runtime
object with no file behind it and goes at the next boot, or to
`systemctl reset-failed`.

-   **The offline package cache takes in §3's install of the package
    set and gives up every deb no part of the set reaches.** The
    cache the old script left in `/data/custom/dpkg` was copied from apt's
    shared archives on some earlier boot. So it can hold debs that belong
    to no part of the set — an `frr` deb above all, which an offline
    post-update boot would install — and it lacks what §3's install
    fetched, which is still in the shared archives. It can also hold a
    package of that install at the version from before it upgraded it.
    Until a firmware update's boot records the firmware base, the new
    `10-packages.sh` keeps every package of the old cache its own download
    does not carry and never reads the shared archives (gateway.md §1.2),
    so none of that is repaired by anything else. Repair it by hand, once,
    now:

        set='systemd-container libnss-mymachines skopeo umoci rsync'
        took=$(awk -v RS= -v set="$set" '
            { n = split($0, l, "\n"); c = ""
              for (i = 1; i <= n; i++)
                  if (l[i] ~ /^Commandline: apt(-get)? / && (l[i] " ") ~ / install /) c = l[i] " "
              m = split(set, w, " ")
              for (i = 1; i <= m; i++) if (c != "" && index(c, " " w[i] " ")) { print; print ""; next } }
            ' /var/log/apt/history.log 2>/dev/null |
            awk '/^(Install|Upgrade): / {
                sub(/^[A-Za-z]+: /, ""); n = split($0, e, /\), /)
                for (i = 1; i <= n; i++) {
                    sub(/\)$/, "", e[i]); sub(/, automatic$/, "", e[i]); split(e[i], f, / \(/)
                    sub(/:.*/, "", f[1]); sub(/.*, /, "", f[2]); print f[1], f[2]
                } }')
        [ -n "$took" ] || echo "history.log holds no apt install of any of $set: nothing copied in"
        printf '%s\n' "$took" | while read -r pkg version; do
            [ -n "$pkg" ] || continue
            [ "$(dpkg-query -W -f '${Version}' "$pkg" 2>/dev/null)" = "$version" ] || continue
            for deb in /var/cache/apt/archives/"$pkg"_*.deb; do
                [ -f "$deb" ] && [ "$(dpkg-deb -f "$deb" Version)" = "$version" ] || continue
                for old in /data/custom/dpkg/"$pkg"_*.deb; do
                    [ -f "$old" ] && [ "${old##*/}" != "${deb##*/}" ] && rm -v "$old"
                done
                [ -f /data/custom/dpkg/"${deb##*/}" ] || cp -v "$deb" /data/custom/dpkg/
            done
        done
        closure=$(apt-cache depends --recurse --no-suggests --no-conflicts \
            --no-breaks --no-replaces --no-enhances $set | grep -v '^ ')
        for deb in /data/custom/dpkg/*.deb; do
            name=${deb##*/}
            printf '%s\n' "$closure" | grep -qxF "${name%%_*}" || rm -v "$deb"
        done

    **What is copied in is what the installs of the set installed**, as
    apt recorded them in `/var/log/apt/history.log`: the `Install:` and
    `Upgrade:` lines of every entry whose `Commandline:` runs `apt` or
    `apt-get` with the word `install` anywhere on it and names at least one
    package of the set. §3's install matches whatever its options and
    their place (`apt-get -y install …`), and so does the set installed a
    few packages at a time. Each package is copied in at the version its
    entry installed, and only while that is still the version installed, so
    a version a later entry superseded is skipped. Any other version of
    that package in the cache is removed, so the cache does not keep an old
    deb of the set beside dependencies at the new version. A firmware
    package that some other apt run upgraded is not in those entries, so it
    is not copied in; one an install of the set upgraded on the way is, and
    so is anything else a command that named the set installed, which the
    removal below drops unless the set reaches it. **The removal** takes every deb whose package
    `apt-cache depends --recurse` does not reach from the set through what
    apt installs by default, recommendations included. That reaches far
    into the firmware, so it removes only what no part of the set needs,
    FRR's debs among them. It reads apt's lists, which §3's install
    fetched; after a firmware update since then, run `apt-get update`
    first.

    The copy-in needs the log entries and the debs. apt writes an entry
    only for a run that changed something, so re-running §3's install once
    the set is in place adds none, and the entries that count are those of
    the runs that installed it. When the log no longer holds any of them (it
    was rotated), the step says so and only removes.
    When the debs are gone from the shared archives (an `apt-get clean`
    or a firmware update since §3), nothing is copied. Either way the cache
    lacks what §3's install fetched until the next online post-update
    boot, and an offline post-update boot before then fails and says so.

    Until the next firmware update's boot, the cache is a superset: it
    never shrinks, and it holds whatever of the set's closure this step
    left in it. **What confirms the new mechanism is the script's own
    line** on the first boot that installs anything. On a boot that finds
    part of the set, such as a push that grows it, the line is
    `packages: no firmware base in /data/custom/dpkg.base/status yet:
    downloading …`. On the first boot after a firmware update, it is
    `packages: none of … is installed on a firmware release the saved base
    is not from … recording it as the base`, then `packages: downloading
    what the firmware base … lacks`. From that update on, the cache is
    exactly what the firmware lacks for the set.
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
-   **A second apply reports no changes and restarts nothing**, which
    is the stamp mechanism proving itself on the path every later apply
    takes. Inside the window that is step 3's command run again,
    targets and all. The same command without them has plenty to
    report here however well the push went — the whole cloud half is
    still uncreated — so the no-change reading over the whole stack is
    unavailable rather than failed, and it is step 5's pass condition
    below.
-   **The stack's exports are readable and worthless, which is worse
    than absent.** An output whose value depends on a resource that
    does not exist yet is written to state as Pulumi's unknown
    sentinel, the literal string
    `04da6b54-80e4-46f7-96ec-b56ff0331ba9`, which
    `pulumi stack output` returns as an ordinary value. A
    `StackReference` reader gets no such string: a preview reads every
    output of the stack as unknown, and an update reads the poisoned
    ones as absent or `None` (framework/pulumi.md §1.4). A targeted
    apply against a stack that has state leaves the outputs of a
    resource outside the target set as state holds them; this one has no state, so **every export whose value
    comes from a resource is of that kind** — the kubeconfig and the
    talosconfig among them. Nothing may read one between step 3 and
    step 5: `credentials derived sync` and each `StackReference`
    reader wait, and nothing in this section reads an export at all.
-   **The routing daemon runs this program's configuration.** The
    installed file is the smaller half of that; the daemon is the rest:

    ```sh
    systemctl is-active frr                            # active
    systemctl list-dependencies --reverse frr.service  # frr-config.service
    grep -x 'bgpd=yes' /etc/frr/daemons                # bgpd=yes
    # frr:frr 640 -- as the stock file already is, so this guards the
    # mode rather than proving the push:
    stat -c '%U:%G %a' /etc/frr/frr.conf
    # the stamp names the source, the release and the parser binary:
    test "$(cat /etc/frr/frr.conf.kluster-applied)" = \
        "$(cksum </data/custom/frr/frr.conf) $(cat /usr/lib/version) $(cksum <"$(command -v vtysh)")" \
        && echo current                                 # current
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

**Step 5 — apply the rest of the stack**, from the operator's
workstation, once every reading above has been taken and every check
with a pass condition has passed, and the resolvers are answering
again. Outside the window, and no longer against a clock:

```sh
physical up
```

It completes the ceremony's step 1 (gateway.md §2.5) — the cloud
fleet, the Talos bootstrap, the worker VM, the backup bucket, the
overlay's network and routes — and re-walks the gateway as a no-op
against the stamps. **It passes when a further `physical up` reports no
changes**, which is the whole-stack form of the reading above and the
one the soak's previews go on repeating. **One diff may appear there
without anything having drifted**: `routes` on
`zerotier:index/network:Network kluster-network`, where the only
difference is `via` on the `10.144.0.0/16` route — declared absent,
read back as `""`. None is expected, since the provider hashes the two
alike; one that appears is that normalization rather than anything
Central did, and the remedy is in this program, not at Central: the
overlay component declares `via=''` for the route the census gives no
`via` (`components/overlay/`), and the reading is taken again once
that lands. A `routes` diff that differs in anything else — a target,
or a `via` naming an address — is not this, and is read as gateway.md
§2.5 step 1 reads the first one. A cloud resource that fails here
fails with the LAN's DNS up and time to spend on it: it is retried
rather than worked around, and nothing about it is a reason to touch
the device. Until this step has passed, step 2 of the ceremony is the
only later one that can run — step 3 reads an address off a machine
this step boots.

**Nothing in §4 or §5 is irreversible**, step 5 included: what it
creates is outside everything §6 undoes, and having it in place
neither closes the rollback nor changes it. §8's `dns` first `up` has
a way back of its own that costs what §8.2 says, and leaves this
section's rollback as it was. The point of no return for the device is
§7:
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
physical stack --show-urns | grep 'URN:' | grep -E \
    'kluster-(persistence|nspawn|caddy|adguard-alice|adguard-bob|zerotier|routing|access)$'
# then, for each URN that printed:
physical state delete --target-dependents '<urn>'
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
    may put back as it ships, and inert with the daemon stopped.
-   `/etc/frr/frr.conf` on the declared content, with its stamp beside
    it. The stock file is not restored, and nothing routes differently
    for it: the peer it declares is the worker VM, which does not exist,
    so the daemon dials an address that never answers and learns no
    route (§2).
-   **One drop-in per machine**, at
    `/etc/systemd/system/systemd-nspawn@<name>.service.d/10-kluster.conf`
    — four of them, carrying `Restart=always` with its five-second
    delay and, for each machine on the container VLAN, the `After=`
    on the bridge's device unit (gateway.md §1). The retiring push
    mirrors `on_boot.d/` and the settings files and knows nothing
    about that directory, and the script that would otherwise remove
    a drop-in nothing declares is itself off the device by then — so
    these outlive the rollback, and the ones for the machines the old
    layout starts again go on amending the very template instances it
    starts them as. That is where "inert" needs its qualification: an
    old machine that starts is unaffected, and one that cannot start
    now retries every five seconds instead of settling in `failed`,
    which is loud in the journal rather than dangerous. The one for
    `zerotier` amends a unit nothing enables. Taking them off is part
    of the rollback rather than of the cleanup after it:

    ```sh
    for m in adguard-alice adguard-bob caddy zerotier; do
        rm -rf "/etc/systemd/system/systemd-nspawn@$m.service.d"
    done
    systemctl daemon-reload
    ```
-   `/data/custom/frr`, one directory holding one configuration file.
-   `machines-new/`, which is where the overlay member's minted identity
    now lives — `machines-new/zerotier/state`. **A retry of the window
    has to carry that directory across**, or the member mints a second
    identity and the node id read in the ceremony's next step is not the
    one the roster was about to authorize.

**Last, the host-side timers come back**, on the homelab host as the
user that owns them, because gw-config is the device's tracker again
and the old layout is what all three were written against:

```sh
systemctl --user enable --now check-gw.timer gw-backup.timer autodeploy-containers.timer
systemctl --user list-timers check-gw.timer gw-backup.timer autodeploy-containers.timer
```

The listing shows the three timers, each with a next run. Without this
step nothing runs the daily drift check or the weekly pull of the UniFi
autobackup and the resolver snapshots, and a homelab-containers merge
reaches the device only when someone deploys it by hand.

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
    `libnss-mymachines`, `skopeo`, `umoci`, and `rsync`, which the pull
    job's own transfer runs on the device end (gateway.md §1.2) —
    and a certificate probe, which now names a declared vhost and reaches
    it with `curl --resolve` rather than through a `lan.ucw.phd` name.
-   **The timer units of the check and the pull**, `check-gw.timer` and
    `gw-backup.timer` (yadm `##h` alternates on the homelab host), keep
    their schedules and lose gw-config from their descriptions; both are
    re-enabled once the scripts above are.
-   **The homelab-ops pin** in yadm's mise configuration moves to the
    commit carrying those scripts, which is what puts them on the host.

**homelab-containers** keeps building and publishing the images and stops
pushing them: a root filesystem is delivered as a digest-pinned artifact
the device pulls itself, so the repository's device-push recipe retires
with a pointer to that, and the `.old` rollback copies it left beside
each tree go with it.

**The reconciler that calls that recipe retires with it**, and it is
not one of the timers that come back. `bin/autodeploy-containers` lives
in homelab-ops and its timer in yadm, so its removal is made of edits
outside this repository:

-   homelab-ops drops the three gateway targets, `adguard-alice`,
    `adguard-bob` and `caddy`, from `bin/autodeploy-containers`, and
    retires the script once no target is left;
-   the yadm `##h` alternates of `autodeploy-containers.timer` and its
    service go with the script;
-   the homelab-ops pin in yadm's mise configuration moves to the commit
    that carries the change.

**This document retires with the window and §8.** It describes a move
that happens once and the bring-up steps that follow it once; the layout
it moves to is described where the device is (gateway.md §1), so the
change that marks rule 0.3's gw-config row done deletes this file as
well, once §8 has run. The procedures §8 uses that run again later are
written where they outlive it, and §8 points at them: replacing CI's
overlay identities in credentials.md §4.1 (stage 10), re-attaching a
node volume through the program in declarative/physical.md §6, and
removing a DS in declarative/dns.md §1.3.

## 8. The rest of the bring-up

The first milestone's steps that follow the ceremony (gateway.md §2.5)
and are not the gateway's. Each runs from the operator's workstation,
in the shell that defined `physical()` at the root of the checkout that
holds `.credentials/`, except where an item says otherwise, and none
of them touches the device or §6's rollback. §8.1 runs once step 5 has
passed and the ceremony's step 3 has written the talosconfig; §8.2 and
§8.3 run in one sitting, after the ceremony's step 4 and after
gateway.md §2.4 has passed, so that no merge lands between them; §8.4
follows §8.2 zone by zone. §3's update window holds for every one of
them.

### 8.1 Verifications that need no network plugin

declarative/physical.md §6 lists the bring-up verifications. Those that
exercise Cilium wait for `k8s-base` and belong to the verification gate
after it (cluster/migration.md §1, item 4 of its build order): the
LB-IPAM pool, the balancer's dual-stack listeners and source
preservation, the reserved address's NAT, the MTU over KubeSpan, the
security verifications, and the volume items that need a pod. The rest
need only the nodes, and run now, while the volumes are empty and a
failure costs nothing. They share one setup:

```sh
TC=~/.talos/kluster          # the talosconfig the ceremony's step 3 wrote
WORKER=192.168.70.10
ip_of() { physical stack output node_public_ips --json | jq -r --arg n "$1" '.[$n]'; }
CP=($(physical stack output node_public_ips --json | jq -r '.[]'))
CPS=$(physical stack output node_public_ips --json | jq -r '[.[]] | join(",")')
tal() { talosctl --talosconfig "$TC" "$@"; }
```

The talosconfig's endpoints are the control planes and its nodes are
the control planes' public addresses and the worker's LAN address, the
same names step 5's health gate uses (`components/talos/`, `TalosDay1`).

**The launches found capacity, and the machine API reaches the worker
through the cloud endpoints.**

```sh
for n in "${CP[@]}" "$WORKER"; do tal -n "$n" version --short; done
```

It passes when every node answers with a server version, and the
version is the `versions:talos` pin in `Pulumi.yaml`. Each control
plane that answers is an A1 launch that found capacity. The worker's
answer is the proxy path: `talosctl` dials only the talosconfig's
endpoints, which are the control planes, and names the worker to them,
so the answer came through whichever control plane it reached.

**etcd's disk.**

```sh
for n in "${CP[@]}"; do printf '%s: ' "$n"; tal -n "$n" logs etcd | grep -c 'slow fdatasync'; done
```

It passes when every control plane prints `0`: etcd logs `slow
fdatasync` for each write-ahead-log sync that takes longer than a
second. That is a tripwire and not cluster/nodes.md §1's criterion,
which is under 10 ms and is read off etcd's
`etcd_disk_wal_fsync_duration_seconds` histogram. No command here reads
that histogram. etcd serves it on its client listener, behind the
client-certificate authentication Talos turns on there, and the machine
configuration's `etcd` block sets no metrics listener of its own
(`components/talos/`), so a reader needs one of the two: a listener
declared, or etcd's client certificate.

**The node volumes.** Each row of `conventions.NODE_VOLUMES` names a
volume and the node it attaches to:

```sh
mise x uv -- uv run python -c 'from kluster import conventions as c; print(*(f"{v.attached_node} {n}" for n, v in c.NODE_VOLUMES.items()), sep="\n")'
```

The rest of this item runs once per line of that listing. A volume's
Talos name is `u-` and the row's name, and each fact is read off the
resource that holds it, inside its `spec` (the resource's metadata
carries a `phase` of its own):

```sh
V=hath-cache; N=$(ip_of cp1)       # one line of the listing above
H=$(for c in "${CP[@]}"; do [ "$c" != "$N" ] && echo "$c" && break; done)   # a control plane that is not N
vol()  { tal -n "$N" get volumestatus "u-$V" -o json | jq -r '.spec.phase'; }
ids()  { tal -n "$N" get discoveredvolumes -o json | jq -r --arg l "u-$V" 'select(.spec.partition_label == $l) | .spec | "\(.name) uuid=\(.uuid) partition_uuid=\(.partition_uuid)"'; }
mnt()  { tal -n "$N" get mountstatus -o json | jq -r --arg t "/var/mnt/$V" 'select(.spec.target == $t) | .spec | "\(.source) \(.target) \(.filesystem)"'; }
boot() { tal -n "$N" read /proc/sys/kernel/random/boot_id; }
vol; ids; mnt; boot
```

1.  **Talos provisioned the disk.** `vol` prints `ready`, `mnt` prints
    the mount at `/var/mnt/<name>`, and `ids` prints the partition's
    filesystem UUID and partition UUID; write both down. Discovery may not have
    probed the partition again since Talos formatted it, so on the boot
    that provisioned it `uuid` can be empty; then item 4's reading is the baseline that item 5 compares
    against.
2.  **Detach it**, in the OCI console, from the instance's attached
    block volumes. `ids` then prints nothing, because the partition has
    left discovery. `vol` still prints `ready`: Talos does not evaluate a
    ready volume again when its disk goes, and that is not a fault.
3.  **Reboot the node with the disk gone**, then read the volume and the
    cluster:

    ```sh
    tal -n "$N" reboot
    vol; boot
    tal -n "$H" health --control-plane-nodes "$CPS" --worker-nodes "$WORKER"
    ```

    `vol` prints a phase other than `ready`, and the health check
    passes: **a user volume still waiting for its disk holds up neither
    the boot nor the health gate.** Write down the boot id.
4.  **Re-attach it through the program**, with the procedure of
    declarative/physical.md §6 for this row's `V`, then read it again:

    ```sh
    vol; ids; mnt; boot
    ```

    `vol` prints `ready`, `mnt` the same mount, `ids` the two UUIDs of
    item 1, and `boot` the id of item 3: **the node took the disk up
    while running, without a reboot, and found its own partition rather
    than provisioning another.** While the procedure there has not
    finished, the volume stays detached, which costs nothing while it
    is empty.
5.  **Reboot once more**, `tal -n "$N" reboot`, then `vol; ids`: `ready`
    and the same two UUIDs. **The volume survives a detach, a re-attach
    and a reboot.** The UUIDs are the reading in place of a sentinel
    file, because they answer the item's question more strongly: a new
    partition draws a new GPT GUID and a format a new filesystem UUID,
    and neither depends on a write having reached the disk before a hot
    detach of a filesystem Talos cannot unmount. What a file would add
    is the kubelet's write path through `/var/mnt`, which is the
    deferred pod item.

**The local-path volume.**

```sh
for n in "${CP[@]}" "$WORKER"; do tal -n "$n" get volumestatus u-storage -o json | jq -r '.spec.phase'; done
```

It passes on `ready` from every node.

**The node label**, on the Node object rather than in the machine
configuration. The kubeconfig is a cluster-admin credential, so it is
written where the talosconfig is kept and not into a working directory,
and the API server answers without a network plugin:

```sh
mkdir -p ~/.kube
(umask 077; physical stack output kubeconfig --show-secrets > ~/.kube/kluster)
L=$(mise x uv -- uv run python -c 'from kluster import conventions as c; print(c.NODE_VOLUME_LABEL)')
kubectl --kubeconfig ~/.kube/kluster get nodes -o wide -L "$L"
physical stack output node_private_ips --json
```

It passes when the label's column holds each row's name on the node whose internal address is the private
address of the row's node, and is empty on every other node.

**The iGPU can be passed through.** This one runs on the homelab host,
and it costs the household something: from `start` to `destroy` below,
the host's `i915` driver lets go of the iGPU, so the legacy cluster's
GPU workloads on that host (immich's machine learning and transcoding,
jellyfin's hardware transcoding) have no device, and a job running on
it fails. It takes a minute or two, at an hour when nobody is watching
anything. The `kubectl` lines run as the operator's own user, whose
`kubectl` context is the legacy cluster's; the rest run as root.

First the readings the way back is measured against:

```sh
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocatable.gpu\.intel\.com/i915}{"\n"}{end}'
kubectl get pods -n intel-gpu -l app=intel-gpu-plugin
lspci -D -nnk -d 8086::0300          # the iGPU's address, and "Kernel driver in use: i915"
```

Write down the homelab node's allocatable count, and confirm that the
device plugin's pod is listed: its namespace and label are what the way
back selects it by. Then the gate, with `GPU` set to the address `lspci`
printed, on its own:

```sh
GPU=0000:00:02.0
ls /sys/bus/pci/devices/$GPU/iommu_group/devices       # the iGPU's own address, and nothing else
```

A missing `iommu_group` directory means the host runs with its I/O
memory management unit off, and a group with other devices in it means
they would have to be passed through with it. Either is a finding
against the host preparation, and nothing below runs. Only a group
holding the iGPU alone goes on to the probe:

```sh
D=$(mktemp -d)
cat > "$D/vfio-probe.xml" <<XML
<domain type='kvm'>
  <name>kluster-vfio-probe</name>
  <memory unit='MiB'>256</memory>
  <vcpu>1</vcpu>
  <os><type arch='x86_64' machine='q35'>hvm</type></os>
  <devices>
    <hostdev mode='subsystem' type='pci' managed='yes'>
      <source><address domain='0x${GPU:0:4}' bus='0x${GPU:5:2}' slot='0x${GPU:8:2}' function='0x${GPU:11:1}'/></source>
    </hostdev>
  </devices>
</domain>
XML
virsh -c qemu:///system define "$D/vfio-probe.xml"
virsh -c qemu:///system start kluster-vfio-probe
virsh -c qemu:///system domstate kluster-vfio-probe     # running
lspci -k -s "$GPU"                                       # Kernel driver in use: vfio-pci
virsh -c qemu:///system destroy kluster-vfio-probe
virsh -c qemu:///system undefine kluster-vfio-probe
rm -rf "$D"
```

It passes when the domain runs and the device is bound to `vfio-pci`
while it does. The domain has no disk and boots nothing, and it does not
need to: a running domain is QEMU holding the device through VFIO,
which is the host's half of the capability. Whether a guest's `i915`
drives the device is read in the cutover itself, where the worker gains
it and the device plugin in the guest reports it (homelab-host.md §3,
cluster/migration.md Wave C). A `start` that fails is read by its
message: one naming VFIO or the I/O memory management unit is a finding against the host
preparation, and one naming the domain's XML is this probe's own shape
at fault. Either way `undefine`, the `rm` and the way back below still
run.

**The way back.** The hostdev is `managed`, so the domain's `destroy`
hands the device back to `i915`. Then:

```sh
lspci -k -s "$GPU"                                     # Kernel driver in use: i915
virsh -c qemu:///system nodedev-reattach "pci_$(echo "$GPU" | tr ':.' '__')"   # only if it still says vfio-pci
ls /dev/dri                                            # the card and render nodes are back
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocatable.gpu\.intel\.com/i915}{"\n"}{end}'
```

The way back is complete when the homelab node's allocatable count is
the one written down. A count of `0` with the device back on `i915` is
the device plugin not having registered the device again: deleting its
pod, `kubectl delete pod -n intel-gpu -l app=intel-gpu-plugin`,
restarts it without evicting the pods that use the GPU, and the count
returns. If `i915` does not take the device back even after
`nodedev-reattach`, the last way back is rebooting the homelab host,
with the legacy cluster and everything else it runs.

### 8.2 The `dns` stack's first `up`

This `up` is the zones' cutover (sources-of-truth.md, row R1): from it
on, the `dns` stack is what writes every zone, and Aetf/dns takes no
push (note N1 there). The stack runs under the stack passphrase that
`mise.toml` hands every run, so it needs no wrapper, and it refuses
until step 5 has published the anchors' addresses (`stacks/dns.py`,
`_usable_address`). From the checkout root:

```sh
mise x -- pulumi preview --stack dns --diff
```

The preview is also the first read of `physical`'s exports by a stack
under the other passphrase. A refusal that names a mapping is that read
failing: it is what a StackReference returns for a secret it cannot
open.

**What the preview must show** is derived from the census rather than
counted, because the counts move with every census change:

-   **No replace anywhere**: no `+-` or `-+` row, and nothing from the
    type move of the zones' component. State holds each zone under the
    component type it had before `ManagedZone` stated its own, and the
    alias carries every one across with nothing replaced
    (Aetf/kluster-ops#478).
-   **The deletes are the records state holds that `zone_records` no
    longer derives**, and nothing else. sources-of-truth.md note N1
    lists them by zone and label, each in a class with the ruling that
    drops it, and its reading B6 prints them from state, taken before
    the preview: every `-` row is a line of B6 and fits a class of N1,
    and every line of B6 is a `-` row.
-   **No `Zone`, `ManagedZone` or `ZoneDnssec` is deleted.** A `Zone` is
    protected, so a run that tries fails there. A `ZoneDnssec` is not,
    and its delete turns DNSSEC off: on a zone whose parent holds a DS,
    that fails the zone for every validating resolver (declarative/dns.md
    §1.3). A `-` on one stops the run.
-   **The co-host has no delete, and its check is negative**, as N1
    states it: unlimitedcodeworks.xyz's create list holds no `*.zt`
    name and no `archvps.hosts` (rfc-003 §5.3), and no `-` row names
    that zone.
-   **The creates**: the CAA records `ZONE_ISSUERS` implies, in each zone
    whose state lacks them (`components/dns/base.py`); a `ZoneDnssec` in
    each zone whose state holds none, which is every zone whose parent
    holds no DS — peifeng.phd, ucw.phd, jiahui.id and jiahui.love, the
    last of which Cloudflare already signs (Aetf/kluster-ops#601); the
    anchors, `kluster.hosts` with an A and an AAAA and `vip1.hosts` with
    an A, in the primary zone; and any other record the census declares
    and state lacks, such as the `*.zt` names of roster members the
    imported block did not carry (Aetf/kluster-ops#510). Beside the
    records, one `kluster:dns:ResolverRewrites` component per entry of
    `conventions.gateway.RESOLVERS`, named `rewrites-<name>`, each
    holding one rewrite per entry that
    `rewrites(conventions.routes.ROUTES)` derives
    (`components/dns/rewrites.py`); with no routes, the components hold
    no rewrite. The one-liner after this list prints what those rows are
    matched against.
-   **The updates** are read row by row: the right side of each field
    is the value of its row in the census.
-   **The anchors carry `physical`'s addresses**: `physical stack output
    cluster_endpoint`, `cluster_endpoint_v6` and `vip1` print the values
    their rows show.

```sh
mise x uv -- uv run python -c 'from kluster import conventions as c; from kluster.components.dns.rewrites import rewrites; print(*(f"rewrites-{r.name}" for r in c.gateway.RESOLVERS)); print(*rewrites(c.routes.ROUTES), sep="\n")'
```

A preview that matches all of that is applied, and the `up` shows the
same plan before it asks:

```sh
mise x -- pulumi up --stack dns
```

**It passes on two readings.** The first is the stack against the
zones:

```sh
mise x -- pulumi preview --stack dns --refresh --expect-no-changes
```

It exits zero, or non-zero with `status` on a `ZoneDnssec` as its
only diff, read back as `pending` against the declared `active`, for a
zone whose DS is not at its parent yet. The provider reads the status
Cloudflare reports, so every zone first signed in §8.2 shows it until
§8.4 has run there. The second is what each
zone's own server answers:

```sh
for z in $(mise x uv -- uv run python -c 'from kluster import conventions as c; print(*c.ALL_ZONES)'); do
  ns="$(drill NS "$z" | awk '/^;/ {next} $4 == "NS" {print $5; exit}')"
  printf '%s\tMX: %s\tDNSKEY: %s\n' "$z" \
    "$(drill MX "$z" @"$ns" | awk '/^;/ {next} $4 == "MX" {printf "%s %s;", $5, $6}')" \
    "$(drill DNSKEY "$z" @"$ns" | awk '/^;/ {next} $4 == "DNSKEY" {printf "%s;", $5}')"
done
z=$(mise x uv -- uv run python -c 'from kluster import conventions as c; print(c.ZONE_PRIMARY)')
ns="$(drill NS "$z" | awk '/^;/ {next} $4 == "NS" {print $5; exit}')"
drill A "kluster.hosts.$z" @"$ns"
drill AAAA "kluster.hosts.$z" @"$ns"
drill A "vip1.hosts.$z" @"$ns"
```

Each zone answers the MX records its census rows declare, every zone
answers DNSKEY records with the flags `256` and `257`, and the anchors
answer the addresses `physical` exports. The DKIM reading, X in
sources-of-truth.md §4, prints one digest on every line.

**The way back is forward.** A row the `up` got wrong is corrected in
the census and applied again; that is the ordinary path from here on,
and it is the only one that keeps the `dns` stack and the zones in
agreement. The earlier declaration exists only as Aetf/dns's
`dnsconfig.js`, and a push to its `master` runs DNSControl over every
zone. It is an emergency measure, and what it costs is the reason it is
one:

-   it recreates every record the `up` deleted, the list in
    sources-of-truth.md note N1;
-   it deletes everything its file does not declare, which includes the
    CAA records and the anchors this `up` created;
-   it leaves DNSSEC as it finds it;
-   and the `dns` stack's state then disagrees with the zones until a
    refresh, so nothing applies `dns` again until it is reconciled. Once
    §8.3 has run, a merge does exactly that, so that way back starts
    with §8.3's own way back.

### 8.3 CI's overlay identities

This is the step that arms CI. Until it runs, no Environment holds a
ZeroTier identity, so the deploy chain stops at `plan-physical` and
nothing a merge starts applies `dns` (sources-of-truth.md §3). Once it
has run, every merge applies `dns` with no reviewer. That is why it
shares a sitting with §8.2, with no merge between them, and why it
waits for gateway.md §2.4: the per-run join it hands CI becomes
load-bearing here (gateway.md §2.5).

The identities are three rows of `credentials derived ls`, and each is
synced alone. A bare `credentials derived sync` fills every row it can
obtain, which here would also issue CI a fresh state-backend client
bundle and write the kubeconfig into `k8s-base`'s and `apps`'s stack
files. From the checkout root:

```sh
mise x uv -- uv run credentials derived sync --only zerotier-network
mise x uv -- uv run credentials derived sync --only zerotier-identity-physical
mise x uv -- uv run credentials derived sync --only zerotier-identity-dns
```

The first puts `conventions.overlay.NETWORK_ID` into the
`ZEROTIER_NETWORK_ID` secret of the `physical-plan`, `physical` and
`dns` Environments; the second puts the `ci-physical` member's identity,
out of `physical`'s state, into `ZEROTIER_IDENTITY` in `physical-plan`
and `physical`; the third puts the `ci-dns` member's into
`ZEROTIER_IDENTITY` in `dns`. Each resolves, pushes and verifies its
row.

**The reading is the drift check**, which previews every stack with
`--refresh --expect-no-changes`, joins the overlay with the identities
just pushed, and applies nothing (`.github/workflows/drift.yml`):

```sh
gh workflow run drift.yml --repo Aetf/kluster
gh run list --workflow drift.yml --repo Aetf/kluster --limit 1 --json databaseId,createdAt,status
gh run watch <databaseId> --repo Aetf/kluster
gh run view <databaseId> --repo Aetf/kluster --json jobs --jq '.jobs[] | .name, (.steps[] | "  \(.conclusion)\t\(.name)")'
gh run view <databaseId> --repo Aetf/kluster --log-failed
```

It passes when the `physical` job succeeds, and the `dns` job either
succeeds or fails in its `Refresh and compare` step with §8.2's
`pending` DNSSEC status as its only diff. The step listing is what
tells the two kinds of red apart: a `dns` job red at its join step
failed to install its identity, and one red at `Refresh and compare`
with that diff alone did not. The `physical` job proves its join: its
preview dials the device over the overlay. The `dns` job proves less.
The action is called with no `wait_for`, so its join step returns once
`zerotier-cli join` does, admitted or not, and while `ROUTES` is empty
the `dns` preview reaches nothing over the overlay. What it shows is
that the identity installs and the stack previews. That `ci-dns` is a
member Central admits is read in Central's member list, which shows it
seen during the run. Its path to the resolvers is first exercised by
the first rewrite the stack writes. Until §8.4 has run for every
zone the `dns` job stays red that way. The `k8s-base` and `apps` jobs
fail, and go on failing until those stacks hold a copy of the
kubeconfig (operations.md §2.5); their failure makes the run red and
starts its alert job, which dispatches into the ops repository, where
nothing handles it yet (operations.md §4).

**The way back** takes the identities out of the Environments, which
stops the deploy chain at `plan-physical` again:

```sh
gh secret delete ZEROTIER_IDENTITY --env dns --repo Aetf/kluster
gh secret delete ZEROTIER_IDENTITY --env physical --repo Aetf/kluster
gh secret delete ZEROTIER_IDENTITY --env physical-plan --repo Aetf/kluster
gh secret list --env dns --repo Aetf/kluster      # and the same for the other two: no ZEROTIER_IDENTITY
```

The network id stays: it opens nothing without an identity. If the
identities themselves are in doubt, they are replaced as credentials.md
§4.1 gives it beside stage 10, which also contains them at Central
first.

### 8.4 The DS records

A zone signed with no DS at its parent is served unvalidated, which is
the state of every zone the `dns` stack signs for the first time in
§8.2. The DS is what validating resolvers then check every answer
against, so **a wrong DS fails the whole zone for every validating
resolver** until it is removed and the parent's TTL for it has run out.
That cost sets the order:

-   unlimited-code.works first, as the control: its parent already
    holds a DS (sources-of-truth.md, reading C2), so the read below must
    produce exactly that DS before it is trusted for any other zone;
-   then peifeng.phd and ucw.phd, the parked zones. This
    installation serves nothing in them, but ucw.phd is also the zone under which the gateway's
    proxy holds its `*.lan.ucw.phd` names (`conventions/dns.py`,
    `PARKED_ZONES`), so a wrong DS there also fails the proxy's DNS-01
    issuance for as long as it stands;
-   then jiahui.id, and only once it has passed, jiahui.love: these two
    carry the family's site and the registrar's mail forwarding.

No DS goes in for a zone whose server does not yet answer its keys.
Every zone above except the control is registered at Namecheap.
jiahui.id's registration names no registrar in its public record, so
if that domain is not in the Namecheap account, its step waits for the
registrar to be found.

**Read the DS from the zone**, at its own server:

```sh
z=unlimited-code.works                  # the control first, then one zone at a time
ns="$(drill NS "$z" | awk '/^;/ {next} $4 == "NS" {print $5; exit}')"
drill -s DNSKEY "$z" @"$ns"
mise x -- pulumi stack export --stack dns | jq -r --arg z "$z" '.deployment.resources[] | select(.urn | endswith("::" + $z + "-dnssec")) | .outputs.ds'
```

`drill -s` prints, after the answer, the DS records equivalent to each
key. The DS is the `; sha256:` line under the key whose answer line
carries the flags `257` (`ksk` in `drill`'s comment): key tag,
algorithm, digest type `2`, digest. The last command prints the DS the
`dns` stack recorded for the zone's `ZoneDnssec`, read out of state
without decrypting anything; the two agree, field for field, the
digest compared without regard to case, since Cloudflare writes it in
upper-case hexadecimal and `drill` in lower-case. On the
control zone, both also agree with what the parent already holds:

```sh
tld=${z##*.}
pns="$(drill NS "$tld" | awk '/^;/ {next} $4 == "NS" {print $5; exit}')"
drill DS "$z" @"$pns"
```

**Enter it at Namecheap**, for every zone but the control: the
domain's Advanced DNS page, its DNSSEC section, a new DS with the key
tag, the algorithm, the digest type and the digest above. A domain
whose page offers no way to add a DS stays signed and unvalidated,
which is where it was before §8.2, and that is recorded on the bring-up
issue rather than worked around.

**Check that it took**, with `tld` and `pns` set for this zone as
above:

```sh
drill DS "$z" @"$pns"                   # the parent's DS: the record entered, and its TTL
drill -D SOA "$z" @1.1.1.1              # flags: ... ad
drill -D SOA "$z" @8.8.8.8              # flags: ... ad
```

It passes when the parent answers the DS that was entered and both
validating resolvers answer `NOERROR` with the `ad` flag set, and that
is what the next zone waits for. The parent can take minutes to publish
what Namecheap saved, and a resolver that cached the zone's earlier
unsigned delegation sets `ad` only once that entry has expired, so a
missing `ad` is waited out. A `SERVFAIL` is not: it is the DS not
matching the zone's key, and the way back runs at once. Write down the
TTL the parent's answer carries; it is how long the way back takes.

Cloudflare clears the zone's `pending` status on a schedule of its own,
usually within the hour, so its DNSSEC panel and the refreshed preview
are read afterward rather than gated on:

```sh
mise x -- pulumi preview --stack dns --refresh --expect-no-changes
```

Once every zone has passed, the panel reads active for each and the
preview is clean.

**The way back** is the DS removed at Namecheap, as declarative/dns.md
§1.3 gives it: the zone stays signed and goes back to unvalidated, and
validating resolvers recover once the parent's TTL for the removed DS
has run out.
