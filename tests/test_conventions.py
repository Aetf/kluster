"""The invariants the convention structures cannot carry in their types.

A dataclass makes a pair that must agree impossible to write apart; what it
cannot express is a relation between the *entries* of a table — a node that
exists, a mount claimed once, an address inside the subnet it belongs to. Those
are checked here, because the tables are static code: nothing can break one at
runtime that this suite did not already catch.
"""

from __future__ import annotations

from ipaddress import IPv6Network

from kluster import conventions

#: The site's /64s as they were written out by hand before the addressing rule
#: derived them. A derivation that stopped matching these would silently
#: renumber the site's firewall rules and resolver rewrites.
KNOWN_PREFIXES = {
    'server': IPv6Network('fd1a:665f:8bcb:80::/64'),
    'iot': IPv6Network('fd1a:665f:8bcb:90::/64'),
    'container': IPv6Network('fd1a:665f:8bcb:5::/64'),
    'cluster': IPv6Network('fd1a:665f:8bcb:70::/64'),
}


def test_a_networks_v6_prefix_is_numbered_after_the_third_octet_of_its_v4() -> None:
    """The site's addressing rule, applied rather than restated.

    Each /64 carries the third octet of its IPv4 subnet as the digits of its
    last group. Deriving it is what makes "a v4 subnet moved and its v6 left
    behind" — rules that match half a network — impossible to declare.
    """
    for network in conventions.SITE_NETWORKS:
        assert network.v6 == KNOWN_PREFIXES[network.name]
        assert network.v6.subnet_of(conventions.SITE_ULA)


def test_the_lan_pool_is_numbered_by_the_same_rule_as_the_networks() -> None:
    """The pool is not a network the gateway serves, but it is at the site.

    Its /64 follows the cluster VLAN's the way its IPv4 subnet follows that
    VLAN's, so the two read as neighbours without ever being one network.
    """
    assert conventions.LAN_POOL.v6 == IPv6Network('fd1a:665f:8bcb:71::/64')
    assert conventions.LAN_POOL.v6.subnet_of(conventions.SITE_ULA)


def test_every_fixed_vip_is_an_address_out_of_the_pool_that_holds_it() -> None:
    """A VIP outside its pool is an address the load balancer can never hand out."""
    for vip in (conventions.LAN_POOL.default_vip, conventions.LAN_POOL.media_vip):
        assert vip.v4 in conventions.LAN_POOL.v4
        assert vip.v6 in conventions.LAN_POOL.v6


def test_a_fixed_vips_two_families_carry_the_same_host_number() -> None:
    """`.1` and `::1` are one address wearing two families.

    The v6 half is written out in full, prefix included, where the pool's own
    /64 is derived — so this is the one place the hand-spelled prefix and the
    host number it carries can drift from the v4 sibling they are meant to
    name. A rewrite pointing at `::2` for what the firewall admits at `.1` is
    a split-horizon answer that reaches the wrong service.
    """
    for vip in (conventions.LAN_POOL.default_vip, conventions.LAN_POOL.media_vip):
        v4_host = int(vip.v4) - int(conventions.LAN_POOL.v4.network_address)
        v6_host = int(vip.v6) - int(conventions.LAN_POOL.v6.network_address)
        assert v4_host == v6_host


def test_every_bridged_service_sits_on_the_container_vlan() -> None:
    """The unit places a service by injecting this address with the VLAN's prefix.

    An address outside the subnet would be configured onto the interface and
    reach nothing, and the resolvers are what every lease on the LAN points at.
    """
    for service in conventions.GW_SERVICES:
        if isinstance(service, conventions.BridgedService):
            assert service.address in conventions.CONTAINER_VLAN.v4


def test_every_volume_is_attached_to_a_node_the_fleet_declares() -> None:
    """A volume attached to a node that does not exist is an apply that half works."""
    for name, volume in conventions.NODE_VOLUMES.items():
        assert volume.attached_node in conventions.CLOUD_NODES, name


def test_no_two_volumes_claim_the_same_mount() -> None:
    """Two volumes at one path is one dataset the node quietly hides."""
    mounts = [volume.mount for volume in conventions.NODE_VOLUMES.values()]
    assert sorted(mounts) == sorted(set(mounts))


def test_no_node_carries_two_volumes() -> None:
    """Checked after the sentinel resolves, which is where the pair can collide.

    Volumes are spread one per node so that the machine configuration's disk
    selection stays "the disk that is not the boot disk" and one node's loss
    takes one preserved dataset rather than two. A following volume landing on
    a node another volume already holds is exactly the state the table can
    reach by an edit somewhere else — `DEDICATED_VIP_NODE`.
    """
    nodes = [volume.attached_node for volume in conventions.NODE_VOLUMES.values()]
    assert sorted(nodes) == sorted(set(nodes))


def test_the_following_volume_is_wherever_the_dedicated_vip_is() -> None:
    """The workload's traffic must leave by the address it arrives on.

    So the volume's node is not a name that could be edited out of step with
    the VIP: it is the sentinel, and it resolves to whatever
    `DEDICATED_VIP_NODE` says today.
    """
    hath = conventions.NODE_VOLUMES['hath-cache']

    assert hath.node is conventions.FOLLOWS_DEDICATED_VIP
    assert hath.attached_node == conventions.DEDICATED_VIP_NODE
    assert conventions.NodeVolume(node='cp3', size_gb=1, mount='/var/mnt/elsewhere').attached_node == 'cp3'


def test_the_block_quota_admits_the_largest_volume_and_a_restore_beside_it() -> None:
    """The quota refuses at creation, so a table that outgrew it never applies.

    The guardrail is a literal of its own on purpose — an envelope this program
    is held to rather than a number derived from what it happens to declare —
    which is exactly why the two have to be compared somewhere. A volume added
    to the table without the envelope following it would not be caught by a
    preview: the refusal arrives from the tenancy, mid-apply, against this
    program's own policy.
    """
    from kluster.components.cloud.guardrails import BLOCK_STORAGE_GB_PER_AD

    largest = max(volume.size_gb for volume in conventions.NODE_VOLUMES.values())
    # One node's boot volume, the largest volume it may carry, and room to
    # restore such a volume beside the one it replaces.
    assert conventions.NODE_BOOT_VOLUME_GB + 2 * largest <= BLOCK_STORAGE_GB_PER_AD
