"""The worker's disk guard as a function (`components.homelab.rebuild`); what the engine does with it is `tests/test_homelab_engine.py`."""

from __future__ import annotations

import pulumi
import pytest

from kluster.components.homelab.rebuild import ImageMissing, SourceGuard


def test_a_guard_whose_run_declared_no_source_refuses_the_delete() -> None:
    args = pulumi.ResourceHookArgs(
        urn='urn:pulumi:s::p::libvirt:index/volume:Volume::disk',
        id='disk',
        name='disk',
        type='libvirt:index/volume:Volume',
    )

    with pytest.raises(ImageMissing) as refused:
        SourceGuard().present(args)

    assert refused.value.path is None
