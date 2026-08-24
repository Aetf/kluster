"""
Base class dealing with the very annoying and grossly incomplete
ComponentResource boilerplate.

Sub-resources are created synchronously in ``__init__`` like plain Pulumi
code; inputs that need async preparation are wrapped with `putils.async_output`.
See docs/rfc-001-native-async-inputs.md.
"""

from typing import Any, ClassVar, Optional

import pulumi

__all__ = ('Component',)


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
    """

    #: Pulumi's type token for the component, defaulted from module and class
    #: name and overridable through the class keyword.
    __pulumi_type__: ClassVar[str]

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

    def child_opts(self, *, opts: Optional[pulumi.ResourceOptions] = None, **kwargs: Any) -> pulumi.ResourceOptions:
        """
        ResourceOptions for a sub-resource: ``parent=self`` plus any extra
        options, merged with `opts` (which wins on conflicts).
        """
        return pulumi.ResourceOptions.merge(pulumi.ResourceOptions(parent=self, **kwargs), opts)
