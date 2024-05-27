"""
Decorator to deal with the very annoying and grossly incomplete
ComponentResource boilerplate.
"""

import asyncio
from typing import Any, Dict, List, Optional, get_type_hints

import pulumi

from .paio import from_nothing, task, unwrap


class Component(pulumi.ComponentResource):
    """
    A ComponentResource with opinioned initialization approach and much less boilerplate.

    If no pulumi_type is given, uses the module and function names.

    All pulumi.Output fields on the object will be immediately available as an output, and will resolve to its value once the initialization is done.

    ```
    class MyComponent(Component, pulumi_type='xxx'):
        an_output: pulumi.Output[int]
        another: pulumi.Output[str]
        yet_another: pulumi.Output[int]

        async def setup(name, *pargs, opts) -> Optional[pulumi.Inputs]:
            async def just_await():
                return 10

            return {
                'an_output': 1,
                'another': pulumi.output(something)
                'yet_another': just_await()
            }
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
        futures: Dict[str, Any] = {}
        # Build out the declared outputs so they're available immediately
        for output_name in self._get_outputs():
            output, futures[output_name] = from_nothing()
            setattr(self, output_name, output)

        # run setup either as async or sync
        if asyncio.iscoroutine(self.setup):
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

    @classmethod
    def _get_outputs(cls) -> List[str]:
        """
        Get a list of output fields of type pulumi.Output of the current class. Fields starting with underscore ('_') are skipped.
        """
        fields = []
        for field_name, field_type in get_type_hints(cls).items():
            if field_name.startswith('_'):
                continue
            if issubclass(field_type, pulumi.Output):
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
        pulumi.Output.all()
        if outs is None:
            outs = {}
        self.register_outputs(outs)
        for name, value in outs.items():
            if futures is not None and name in futures:
                futures[name].set_result(value)
            else:
                setattr(self, name, value)

    def setup(self, name: str, *pargs, opts: Optional[pulumi.ResourceOptions], **kwargs) -> Optional[pulumi.Inputs]:
        pass
