"""The refusal that keeps the worker's disk when its replacement has no file to be created from (operations.md §3.2).

The disk is created from a file on the machine running the program, and the
pinned libvirt provider first opens that file at the disk's create
(terraform-provider-libvirt v0.8.1, `libvirt/volume_image.go`, `IsQCOW2`),
while the disk is deleted before it is replaced. A replacement with no file at
the path would therefore delete the disk and then fail, and leave every later
`up` pending a replacement it cannot make. Once the image's resource exists,
what puts a lost file back is its replacement, named in the same run.

`SourceGuard` refuses that delete from the program's side. It declares the
disk's `source`, recording the path the program declares in this run, and its
`before_delete` hook on the disk raises `ImageMissing` when no file is at that
path. The path checked is the declared one, not the one in state: a delete
hook is handed the inputs the disk was created with, and after a Talos bump
those name an image the rebuild does not put back.

The hook runs only on a step of the disk, and the disk's steps begin once it
registers, which waits on `source`; so whenever the hook runs, the guard holds
this run's path, and in the rebuild the image's replacement, which `source`
waits on, has already written the file.

What the refusal keeps is the disk. The seed and the domain depend on it and
are deleted before it, so the worker is down either way, and a plain `up` then
creates the two again on the kept disk. It covers a missing file; a create
that fails for any other reason still fails after the delete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pulumi

from putils import async_output, resolve

__all__ = ('ImageMissing', 'SourceGuard')


class ImageMissing(RuntimeError):
    """The disk's `before_delete` hook refused: no file is at the path its replacement would be created from.

    `path` is that path as this run declared it, or `None` where the run
    declared none. The refusal names the run that puts the file there.
    """

    def __init__(self, message: str, *, path: str | None) -> None:
        super().__init__(message)
        self.path: str | None = path


@dataclass
class SourceGuard:
    """The disk's `source`, and the hook that refuses to delete the disk when nothing is there.

    One instance per disk and per program run: the path it holds is the one
    this run declared.
    """

    _declared: str | None = field(default=None, init=False, repr=False)

    def source(self, image_path: pulumi.Input[str]) -> pulumi.Output[str]:
        """The disk's `source`: `image_path`, recorded here once it resolves, and waiting on what writes it."""

        async def declared() -> str:
            path = await resolve(image_path)
            self._declared = path
            return path

        return async_output(declared)

    def hooks(self, name: str) -> pulumi.ResourceHookBinding:
        """The disk's binding: `present` before every delete, registered under `name`.

        **The binding is recorded in state** with the disk, and a delete runs
        the hooks state recorded rather than the ones the deleting run binds.
        So the guard holds on a disk once an `up` has run with it, any plain
        `up` included, and not before. The hook is called by its recorded
        name, and a delete fails with `hook "…" was not registered` where the
        program registers none of that name: it is a name state remembers,
        like a resource's.
        """
        return pulumi.ResourceHookBinding(before_delete=[pulumi.ResourceHook(name, self.present)])

    def present(self, args: pulumi.ResourceHookArgs) -> None:
        """Refuse the disk's delete unless a file is at the path this run declared.

        A relative path is read against the program's working directory, which
        is the provider's as well (`connection_uri`).
        """
        path = self._declared
        if path is None:
            raise ImageMissing(
                f'this run declared no source for {args.urn}, so nothing shows its replacement could be created, '
                'and the disk is kept',
                path=None,
            )
        if not Path(path).is_file():
            raise ImageMissing(
                f'no file is at {path}, which {args.urn} would be created from again, so the disk is kept rather '
                "than deleted ahead of a create that cannot open it: `physical up --replace '<image URN>' "
                f"--replace '{args.urn}'` puts the file there before the disk is deleted, the image being the "
                'resource that writes it (operations.md §3.2)',
                path=path,
            )
