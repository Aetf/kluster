"""
Base class dealing with the very annoying and grossly incomplete
ComponentResource boilerplate, and the backstop that keeps a component's
sub-resources attached to it.

Sub-resources are created synchronously in ``__init__`` like plain Pulumi
code; inputs that need async preparation are wrapped with `putils.async_output`.
See docs/rfc-001-native-async-inputs.md.
"""

from __future__ import annotations

import contextvars
import weakref
from dataclasses import dataclass
from typing import Any, ClassVar, Optional

import pulumi
import pulumi.runtime

__all__ = (
    'Component',
    'UnparentedChildError',
    'install_parent_backstop',
    'own_provider_opts',
    'with_provider',
)


@dataclass(frozen=True)
class _Frame:
    """One component whose ``__init__`` is running: how a refusal names it."""

    name: str
    type_: str

    def __str__(self) -> str:
        return f'{self.name} ({self.type_})'


#: The components under construction, outermost first. `Component.__init__`
#: appends itself *after* its own registration, so a component is never inside
#: its own scope, and `Component.register_outputs` truncates it back off.
#:
#: A ContextVar rather than a plain global because a coroutine started inside a
#: component's ``__init__`` gets a copy of the context it was created in: work
#: deferred onto the event loop keeps the scope it was written in instead of
#: inheriting whatever component happens to be building when it runs.
_under_construction: contextvars.ContextVar[tuple[_Frame, ...]] = contextvars.ContextVar(
    'putils_under_construction', default=()
)

#: The stack resources the backstop is already registered on, so that calling
#: `install_parent_backstop` twice in one program installs one transformation.
#: Weak, because a test process goes through a root resource per test.
_installed_on: weakref.WeakSet[pulumi.Resource] = weakref.WeakSet()


class UnparentedChildError(Exception):
    """A resource was registered inside a component without naming a parent."""


def _refuse_unparented(
    args: pulumi.ResourceTransformationArgs,
) -> Optional[pulumi.ResourceTransformationResult]:
    """Refuse a resource registered inside a component with no parent set.

    A stack transformation cannot repair this — the SDK raises
    ``Transformations cannot currently be used to change the 'parent' of a
    resource``, because the parent is what selects which transformations run in
    the first place — so it refuses instead, naming both halves of the mistake.
    """
    enclosing = _under_construction.get()
    if not enclosing or args.opts.parent is not None:
        return None
    raise UnparentedChildError(
        f'{args.type_} "{args.name}" is declared inside component {enclosing[-1]} but names no parent. '
        f'Pass opts=self.child_opts() (or an explicit parent): a resource with no parent belongs to the '
        f"stack rather than to the component, and inherits the stack's providers instead of the "
        f"component's. A transformation cannot set the parent for you, so this is refused rather than "
        f'fixed.'
    )


def install_parent_backstop() -> None:
    """Register the parent backstop for the rest of this program.

    One stack transformation, inherited by every resource the program goes on
    to declare, which refuses any resource registered while a component is
    under construction whose options name no parent (rfc-002 §8.2). It has to
    be called from inside a running Pulumi program — a stack transformation
    hangs off the root stack resource, which only exists there — and before the
    first component is built, since a resource only carries the
    transformations that existed when its parent was constructed.

    Calling it more than once against the same stack is a no-op.

    What it does *not* catch: a resource declared outside any component, which
    is a stack program's business and legitimate; and anything declared after a
    component that never called ``register_outputs``, which is the failure mode
    documented on `Component.register_outputs`.
    """
    root = pulumi.runtime.get_root_resource()
    if root is None:
        raise RuntimeError(
            'install_parent_backstop() needs a root stack resource: call it from inside a running '
            'Pulumi program (or, in a test, after pulumi.runtime.set_mocks)'
        )
    if root in _installed_on:
        return
    _installed_on.add(root)
    pulumi.runtime.register_stack_transformation(_refuse_unparented)


def own_provider_opts(opts: Optional[pulumi.ResourceOptions]) -> pulumi.ResourceOptions:
    """Options for the provider a component builds for itself.

    A component that owns a connection builds its provider *before* its own
    ``super().__init__``, because a provider is handed to a subtree through the
    component's own options and those are fixed at registration. So the
    provider is not the component's child — it is its sibling, and it takes the
    parent the component was given. Under the parent backstop that is also what
    makes it legal: a provider built inside an enclosing component with no
    parent of its own would be refused like any other unparented resource.

    Nothing else of the component's options travels with it: a provider is not
    protected, replaced or ordered by what the component it serves is.
    """
    return pulumi.ResourceOptions(parent=opts.parent if opts is not None else None)


def with_provider(opts: Optional[pulumi.ResourceOptions], provider: pulumi.ProviderResource) -> pulumi.ResourceOptions:
    """A component's own options, carrying `provider` for its whole subtree.

    This is how a provider reaches the resources under a component without any
    of them naming it (rfc-002 §8.1): a provider in a component's `providers`
    map is inherited by every child that names the component as its parent, and
    transitively by their children, with the first match by package name
    winning. `child_opts(provider=...)` therefore belongs nowhere in a
    component body — the provider is stated once, where it is built.
    """
    return pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(providers=[provider]))


class Component(pulumi.ComponentResource):
    """
    A ComponentResource with opinioned initialization approach and much less boilerplate.

    If no pulumi_type is given, uses the module and class names.

    Subclasses override ``__init__``, call ``super().__init__`` first, then
    create sub-resources synchronously. Async input preparation is wrapped
    in `putils.async_output`:

    ```
    class MyComponent(Component):
        def __init__(self, name: str, opts: pulumi.ResourceOptions | None = None):
            super().__init__(name, opts=opts)
            self.vpc = Network(f'{name}-vpc', opts=self.child_opts())
            self.subnet = Subnetwork(
                f'{name}-subnet',
                cidr='10.0.1.0/24',
                network_id=async_output(self._network_id),
                opts=self.child_opts(protect=True),
            )
            self.register_outputs({})

        async def _network_id(self) -> str:
            vpc_id = await resolve(self.vpc.id)
            return f'prefix-{vpc_id}'
    ```

    The `super().__init__` … `register_outputs` pair is also the scope the
    parent backstop enforces: between them, a resource declared without a
    parent is refused instead of silently landing on the stack. See
    `install_parent_backstop`.
    """

    #: Pulumi's type token for the component, defaulted from module and class
    #: name and overridable through the class keyword.
    __pulumi_type__: ClassVar[str]

    #: This component's entry in the under-construction scope, held so that
    #: `register_outputs` pops the right one out of a nested stack of them.
    _putils_frame: _Frame

    @classmethod
    def __init_subclass__(cls, *, pulumi_type: Optional[str] = None, **kwargs: object):
        super().__init_subclass__(**kwargs)
        if pulumi_type is not None:
            cls.__pulumi_type__ = pulumi_type
        elif not hasattr(cls, '__pulumi_type__'):
            cls.__pulumi_type__ = f'{cls.__module__}:{cls.__qualname__}'.replace('.', ':')

    def __init__(self, name: str, opts: Optional[pulumi.ResourceOptions] = None):
        """
        :param str name: The name of this resource.
        :param Optional[ResourceOptions] opts: Optional set of :class:`pulumi.ResourceOptions` to use for this
               resource.
        """
        super().__init__(self.__pulumi_type__, name=name, props=None, opts=opts)
        # After the registration above, deliberately: a component is not its
        # own child, so its own `parent=` is judged against whatever component
        # encloses *it*. A top-level component built by a stack program is
        # enclosed by nothing and needs no parent; one built inside another
        # component does, exactly like any other resource there.
        self._putils_frame = _Frame(name, self.__pulumi_type__)
        _under_construction.set((*_under_construction.get(), self._putils_frame))

    def register_outputs(self, outputs: pulumi.Inputs) -> None:
        """
        Close the component: publish its outputs and leave the parent
        backstop's scope.

        Every component calls this as the last statement of its ``__init__``.
        A component that forgets stays on the scope stack, and the next
        resource declared without a parent — including a sibling component the
        stack program builds afterwards — is refused in its name rather than
        its own. The refusal is then misleading but never absent, and it is
        bounded: an enclosing component's own `register_outputs` truncates the
        leaked entry away with itself.
        """
        frames = _under_construction.get()
        for index, frame in enumerate(frames):
            if frame is self._putils_frame:
                _under_construction.set(frames[:index])
                break
        super().register_outputs(outputs)

    def child_opts(self, *, opts: Optional[pulumi.ResourceOptions] = None, **kwargs: Any) -> pulumi.ResourceOptions:
        """
        ResourceOptions for a sub-resource: ``parent=self`` plus any extra
        options, merged with `opts` (which wins on conflicts).
        """
        return pulumi.ResourceOptions.merge(pulumi.ResourceOptions(parent=self, **kwargs), opts)
