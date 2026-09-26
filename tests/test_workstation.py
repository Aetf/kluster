"""The modes `kluster.lib.workstation` puts on what it writes, and the checkout it finds.

A secret is `0600` from the moment it exists, which is a property of how the
file is created and not of what happens to it afterwards: a file written first
and narrowed second is readable by anyone the umask let in for as long as the
gap lasts. So the cases on `write` run under a umask of zero with every
`chmod` made a no-op. What is left is the mode the file was created with, and a
writer that relies on narrowing it afterwards leaves a world-readable file
behind here instead of a gap nobody sees.

The directory half is about a `.credentials/` that a copy brought in wider
than it should be, and about every directory outside that tree, which is the
operator's and is not touched.

The root half is about which checkout all of that is relative to, and that
every reader of the checkout finds the same one.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from kluster.lib import workstation
from kluster.scripts.credentials import pulumi_config
from kluster.scripts.state_backend import config as appliance


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def open_umask() -> Iterator[None]:
    """A umask that takes nothing away, restored afterwards: the widest a file can be created."""
    previous = os.umask(0)
    try:
        yield
    finally:
        _ = os.umask(previous)


@pytest.fixture
def no_chmod(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every way to change a mode after creation, made to do nothing."""

    def ignored(*args: object, **kwargs: object) -> None:
        _ = args, kwargs

    monkeypatch.setattr(os, 'chmod', ignored)
    monkeypatch.setattr(os, 'fchmod', ignored)


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout root of the test's own, so `.credentials/` is under `tmp_path`."""
    monkeypatch.setattr(workstation, 'repo_root', lambda: tmp_path)
    return tmp_path


@pytest.mark.usefixtures('open_umask', 'no_chmod')
def test_a_secret_is_created_0600_rather_than_narrowed_afterwards(tmp_path: Path) -> None:
    slot = tmp_path / 'slot' / 'passphrase'

    _ = workstation.write(slot, 'a-secret')

    assert _mode(slot) == 0o600
    assert slot.read_text() == 'a-secret\n'


@pytest.mark.usefixtures('open_umask', 'no_chmod')
def test_a_wider_file_already_in_the_slot_is_replaced_rather_than_written_into(tmp_path: Path) -> None:
    # Writing into the existing file would keep its mode, and the new secret
    # would land in a file anyone can read.
    slot = tmp_path / 'passphrase'
    _ = slot.write_text('the-old-one\n')
    slot.chmod(0o644)

    _ = workstation.write(slot, 'the-new-one')

    assert _mode(slot) == 0o600
    assert slot.read_text() == 'the-new-one\n'


def test_nothing_but_the_slot_is_left_beside_it(tmp_path: Path) -> None:
    slot = tmp_path / 'passphrase'

    _ = workstation.write(slot, 'a-secret')
    _ = workstation.write(slot, 'another')

    assert [path.name for path in tmp_path.iterdir()] == ['passphrase']


def test_a_missing_level_is_created_0700(tmp_path: Path) -> None:
    slot = tmp_path / 'one' / 'two' / 'passphrase'

    _ = workstation.write(slot, 'a-secret')

    assert _mode(tmp_path / 'one') == 0o700
    assert _mode(tmp_path / 'one' / 'two') == 0o700


def test_an_existing_credentials_tree_is_narrowed_to_its_owner(checkout: Path) -> None:
    # A `.credentials/` copied in from another machine keeps the mode the
    # copy gave it; the first write into it is what holds it to `0700`.
    tree = checkout / workstation.DIRECTORY
    nested = tree / 'state-backend'
    nested.mkdir(parents=True)
    tree.chmod(0o755)
    nested.chmod(0o775)
    checkout.chmod(0o755)

    _ = workstation.write(nested / 'client.key', 'a-key')

    assert _mode(tree) == 0o700
    assert _mode(nested) == 0o700
    # The checkout above it is not this module's to change.
    assert _mode(checkout) == 0o755


def test_a_checkout_reached_through_a_symlink_still_has_its_tree_narrowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tree is compared on resolved paths, so the checkout's own path has to
    # be resolved too: a checkout found through a link would otherwise never
    # contain its own `.credentials/`.
    real = tmp_path / 'real'
    real.mkdir()
    linked = tmp_path / 'linked'
    linked.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(workstation, 'repo_root', lambda: linked)
    tree = real / workstation.DIRECTORY
    tree.mkdir()
    tree.chmod(0o755)

    _ = workstation.secret_dir(real / workstation.DIRECTORY)

    assert _mode(tree) == 0o700


def test_a_directory_outside_the_credentials_tree_is_left_as_it_is(checkout: Path) -> None:
    # A kit may sit anywhere the operator points it, and the directory it sits
    # in is theirs.
    elsewhere = checkout / 'media'
    elsewhere.mkdir()
    elsewhere.chmod(0o755)

    made = workstation.secret_dir(elsewhere / 'kit')

    assert _mode(elsewhere) == 0o755
    assert _mode(made) == 0o700


def test_a_link_out_of_the_credentials_tree_does_not_carry_the_narrowing_with_it(
    checkout: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    # `chmod` follows a symlink, so a link inside the tree that points at a
    # directory the operator owns elsewhere must not be read as part of it.
    media = tmp_path_factory.mktemp('media')
    media.chmod(0o755)
    tree = checkout / workstation.DIRECTORY
    tree.mkdir(mode=0o700)
    (tree / 'kit').symlink_to(media, target_is_directory=True)

    _ = workstation.secret_dir(tree / 'kit')

    assert _mode(media) == 0o755


def test_a_path_that_climbs_out_of_the_credentials_tree_narrows_nothing_outside_it(checkout: Path) -> None:
    tree = checkout / workstation.DIRECTORY
    tree.mkdir(mode=0o700)
    sibling = checkout / 'sibling'
    sibling.mkdir()
    sibling.chmod(0o755)
    checkout.chmod(0o755)

    _ = workstation.secret_dir(tree / '..' / 'sibling')

    assert _mode(sibling) == 0o755
    assert _mode(checkout) == 0o755


def test_a_write_that_fails_leaves_no_staged_file_behind(tmp_path: Path) -> None:
    # A directory in the slot's place makes the rename fail after the staged
    # file exists, which is the case the cleanup is for.
    slot = tmp_path / 'passphrase'
    slot.mkdir()

    with pytest.raises(IsADirectoryError):
        _ = workstation.write(slot, 'a-secret')

    assert [path.name for path in tmp_path.iterdir()] == ['passphrase']


def test_the_staged_file_is_made_beside_the_slot_rather_than_in_the_temporary_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A rename is atomic only within one filesystem, and the default
    # temporary directory is usually on another one; one that does not exist
    # makes relying on it fail here rather than only on such a machine.
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path / 'no-such-directory'))
    slot = tmp_path / 'slot' / 'passphrase'

    _ = workstation.write(slot, 'a-secret')

    assert slot.read_text() == 'a-secret\n'


def _checkout(root: Path) -> Path:
    """A checkout at `root`: the markers the root is found by, and a module inside it."""
    root.mkdir(parents=True, exist_ok=True)
    _ = (root / 'mise.toml').write_text('')
    _ = (root / 'Pulumi.yaml').write_text('')
    module = root / 'src' / 'kluster' / 'lib' / 'workstation.py'
    module.parent.mkdir(parents=True)
    _ = module.write_text('')
    return module


def test_a_checkout_nested_in_another_is_its_own_root(tmp_path: Path) -> None:
    # A workspace under the primary's `.claude/workspaces/` carries markers of
    # its own, and the code running from it is its own: the slots, the config
    # files and the deployment material it reads are the workspace's.
    _ = _checkout(tmp_path / 'primary')
    nested = tmp_path / 'primary' / '.claude' / 'workspaces' / 'feature'
    module = _checkout(nested)

    assert workstation.repo_root(module) == nested


def test_a_module_outside_any_checkout_is_refused(tmp_path: Path) -> None:
    if any((level / 'mise.toml').is_file() for level in tmp_path.parents):
        pytest.skip('the temporary directory is itself inside a checkout')
    module = tmp_path / 'site-packages' / 'kluster' / 'lib' / 'workstation.py'
    module.parent.mkdir(parents=True)
    _ = module.write_text('')

    with pytest.raises(workstation.WorkstationError, match=re.escape('no mise.toml above')):
        _ = workstation.repo_root(module)


def test_every_reader_of_the_checkout_finds_the_one_this_package_runs_from() -> None:
    # Run from a workspace nested in the primary checkout, this is the nested
    # case on the real tree: the root is the tree holding the running code, not
    # the checkout around it.
    root = workstation.repo_root()

    assert (root / 'src' / 'kluster' / 'lib' / 'workstation.py').samefile(workstation.__file__)
    assert pulumi_config.project_dir() == root
    assert appliance.DEPLOY_DIR.is_relative_to(root)
    assert (appliance.DEPLOY_DIR / appliance.TEMPLATE).is_file()
