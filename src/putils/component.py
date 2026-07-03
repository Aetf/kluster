"""
Decorator to deal with the very annoying and grossly incomplete
ComponentResource boilerplate.

Sub-resources are created synchronously in ``__init__`` like plain Pulumi
code; inputs that need async preparation are wrapped with `putils.async_output`.
See docs/rfc-001-native-async-inputs.md.
"""

import inspect
import sys
from typing import Any, Dict, List, Optional, get_origin

import pulumi

from .paio import from_nothing, task, unwrap

__all__ = ('Component',)


class Component(pulumi.ComponentResource):
    """
    A ComponentResource with opinioned initialization approach and much less boilerplate.

    If no pulumi_type is given, uses the module and class names.

    New-style components override ``__init__``, call ``super().__init__`` first,
    then create sub-resources synchronously. Async input preparation is wrapped
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

    Legacy-style components instead declare ``pulumi.Output`` annotations and
    override ``setup`` (sync or async), returning a dict resolving them:

    ```
    class MyComponent(Component, pulumi_type='xxx'):
        an_output: pulumi.Output[int]

        async def setup(self, name, *pargs, opts, **kwargs) -> Optional[pulumi.Inputs]:
            return {'an_output': 1}
    ```
    """

    @classmethod
    def __init_subclass__(cls, *, pulumi_type: Optional[str] = None, **kwargs):
        super().__init_subclass__(**kwargs)
        if pulumi_type is not None:
            cls.__pulumi_type__ = pulumi_type
        elif not hasattr(cls, '__pulumi_type__'):
            cls.__pulumi_type__ = f'{cls.__module__}:{cls.__qualname__}'.replace('.', ':')

    def __init__(self, name: str, *pargs, opts: Optional[pulumi.ResourceOptions] = None, **kwargs):
        """
        :param str name: The name of this resource.
        :param Optional[ResourceOptions] opts: Optional set of :class:`pulumi.ResourceOptions` to use for this
               resource.
        """
        super().__init__(self.__pulumi_type__, name=name, props=None, opts=opts)

        if type(self).setup is Component.setup:
            # New-style component: the subclass __init__ creates sub-resources
            # itself after this returns.
            return

        futures: Dict[str, Any] = {}
        # Build out the declared outputs so they're available immediately
        for output_name in self._get_outputs():
            output, futures[output_name] = from_nothing()
            setattr(self, output_name, output)

        # run setup either as async or sync
        if inspect.iscoroutinefunction(self.setup):
            self._inittask(futures, name, *pargs, opts=opts, **kwargs)
        else:
            try:
                outs = self.setup(name, *pargs, opts=opts, **kwargs)
            except Exception as e:
                for f in futures.values():
                    f.set_exception(e)
                raise
            else:
                self._process_outs(outs, futures)

    def child_opts(self, *, opts: Optional[pulumi.ResourceOptions] = None, **kwargs) -> pulumi.ResourceOptions:
        """
        ResourceOptions for a sub-resource: ``parent=self`` plus any extra
        options, merged with `opts` (which wins on conflicts).
        """
        return pulumi.ResourceOptions.merge(pulumi.ResourceOptions(parent=self, **kwargs), opts)

    @classmethod
    def _get_outputs(cls) -> List[str]:
        """
        Get a list of output fields of type pulumi.Output declared on Component
        subclasses. Fields starting with underscore ('_') are skipped.

        Only annotations on classes below Component in the MRO are considered,
        so pulumi's own internal annotations never need resolving.
        """
        fields = []
        for klass in cls.__mro__:
            if klass is Component:
                break
            module_ns = getattr(sys.modules.get(klass.__module__, None), '__dict__', {})
            for field_name, ann in vars(klass).get('__annotations__', {}).items():
                if field_name.startswith('_') or field_name in fields:
                    continue
                if isinstance(ann, str):
                    try:
                        ann = eval(ann, dict(module_ns))  # noqa: S307
                    except Exception:
                        continue
                base = get_origin(ann) or ann
                if isinstance(base, type) and issubclass(base, pulumi.Output):
                    fields.append(field_name)
        return fields

    @task
    async def _inittask(self, futures, *pargs, **kwargs):
        # Wraps up the initialization function and marshalls the data around
        try:
            # Call the initializer
            outs = await unwrap(self.setup(*pargs, **kwargs))
        except Exception as e:
            # Forward the exception to the futures, so they don't hang
            for f in futures.values():
                f.set_exception(e)
            raise
        else:
            self._process_outs(outs, futures)

    def _process_outs(self, outs: Optional[pulumi.Inputs], futures=None):
        """Process outputs in `outs`.

        This registers outputs with pulumi, and the value either
          * ressolves the associated output future, or
          * pass to setattr on `self`
        """
        if outs is None:
            outs = {}
        self.register_outputs(outs)
        for name, value in outs.items():
            if futures is not None and name in futures:
                futures[name].set_result(value)
            else:
                setattr(self, name, value)

    def setup(self, name: str, *pargs, opts: Optional[pulumi.ResourceOptions], **kwargs) -> Optional[pulumi.Inputs]:
        """
        Setup the custom component (legacy flow). See the class docstring.
        """
        pass
