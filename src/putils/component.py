"""
Decorator to deal with the very annoying and grossly incomplete
ComponentResource boilerplate.
"""

import asyncio
import contextvars
import inspect
from typing import Any, Dict, List, Optional, Union, get_type_hints, get_origin, get_args

import pulumi
from pulumi import Output, UNKNOWN

# Workaround for Pulumi's type hint resolution issue (go/pulumi-python-type-hints)
import pulumi.resource
if not hasattr(pulumi.resource, 'Input'):
    pulumi.resource.Input = pulumi.Input
if not hasattr(pulumi.resource, 'Output'):
    pulumi.resource.Output = pulumi.Output
if not hasattr(pulumi.resource, 'ProviderResource'):
    import pulumi.provider
    pulumi.resource.ProviderResource = pulumi.provider.ProviderResource
if not hasattr(pulumi.resource, 'ResourceTransformation'):
    pulumi.resource.ResourceTransformation = Any
from typing import TypeVar
if not hasattr(pulumi.resource, 'T'):
    pulumi.resource.T = TypeVar('T')

from .paio import from_nothing, task, unwrap


class UnknownValueException(Exception):
    """Exception raised when an unknown output is resolved during preview."""
    pass


# Context variable containing a mutable set instance shared across standard asyncio task hierarchies
current_task_dependencies = contextvars.ContextVar("current_task_dependencies", default=None)


class AwaitableOutput:
    def __init__(self, *outputs: Output):
        self.outputs = outputs

    def __await__(self):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        deps_set = current_task_dependencies.get()
        for out in self.outputs:
            if isinstance(out, Output):
                resources = yield from out.resources().__await__()
                if deps_set is not None:
                    deps_set.update(resources)

        all_output = Output.all(*self.outputs)

        def on_resolved(values):
            any_unknown = any((type(v).__name__ == 'Unknown') or (v is UNKNOWN) for v in values)
            if any_unknown and pulumi.runtime.is_dry_run():
                loop.call_soon_threadsafe(
                    lambda: fut.set_exception(UnknownValueException()) if not fut.done() else None
                )
            else:
                res_val = values[0] if len(self.outputs) == 1 else tuple(values)
                loop.call_soon_threadsafe(
                    lambda: fut.set_result(res_val) if not fut.done() else None
                )

        all_output.apply(on_resolved, run_with_unknowns=True)

        val = yield from fut.__await__()
        return val


def resolve(*outputs: Output):
    return AwaitableOutput(*outputs)


def setup(resource_name: str, protect: bool = False, depends_on: Optional[List[str]] = None, keys: Optional[List[str]] = None):
    def decorator(func):
        func._is_setup = True
        func._resource_name = resource_name
        func._protect = protect
        func._depends_on = depends_on or []
        func._explicit_keys = keys
        return func
    return decorator


def _extract_keys_from_type(type_hint) -> Optional[List[str]]:
    if type_hint is None:
        return None
    if hasattr(type_hint, "__annotations__"):
        return list(type_hint.__annotations__.keys())
    origin = get_origin(type_hint)
    if origin is not None:
        args = get_args(type_hint)
        if args:
            return _extract_keys_from_type(args[0])
    return None


def _topological_sort(nodes: List[str], edges: Dict[str, List[str]]) -> List[str]:
    visited = {}
    result = []

    def visit(node):
        if visited.get(node) == 1:
            raise ValueError(f"Cyclic dependency detected involving '{node}'")
        if visited.get(node) == 2:
            return
        visited[node] = 1
        for dep in edges.get(node, []):
            if dep in nodes:
                visit(dep)
        visited[node] = 2
        result.append(node)

    for node in nodes:
        visit(node)
    return result


class Component(pulumi.ComponentResource):
    """
    A ComponentResource with opinioned initialization approach and much less boilerplate.

    If no pulumi_type is given, uses the module and function names.
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
        
        # 1. Discover annotated sub-resources
        sub_resources = {}
        for field_name, field_type in get_type_hints(self.__class__).items():
            if field_name.startswith('_'):
                continue
            if isinstance(field_type, type) and issubclass(field_type, pulumi.Resource):
                sub_resources[field_name] = field_type

        # 2. Discover @setup methods on the class to avoid triggering Output __getattr__ proxying
        setup_methods = {}
        for attr_name in dir(self.__class__):
            attr = getattr(self.__class__, attr_name)
            if hasattr(attr, "_is_setup") and getattr(attr, "_is_setup"):
                res_name = getattr(attr, "_resource_name")
                setup_methods[res_name] = getattr(self, attr_name)

        # 3. Topologically sort sub-resources based on depends_on
        depends_on_map = {}
        for res_name, setup_method in setup_methods.items():
            depends_on_map[res_name] = getattr(setup_method, "_depends_on", [])
        
        sorted_res_names = _topological_sort(list(sub_resources.keys()), depends_on_map)

        # 4. Instantiate sub-resources synchronously in topological order
        sub_resource_futures = {}
        self._sub_resource_deps_futures = {}

        for res_name in sorted_res_names:
            child_class = sub_resources[res_name]
            setup_method = setup_methods.get(res_name)
            if setup_method is None:
                raise ValueError(f"Sub-resource '{res_name}' is declared but has no corresponding @setup method.")

            # Discover keys
            keys = getattr(setup_method, "_explicit_keys", None)
            if keys is None:
                hints = get_type_hints(setup_method)
                return_hint = hints.get("return")
                keys = _extract_keys_from_type(return_hint)
            
            if keys is None:
                raise ValueError(
                    f"Setup method '{setup_method.__name__}' for sub-resource '{res_name}' "
                    f"must specify return type annotation (TypedDict) or 'keys' in the @setup decorator."
                )

            res_futs = {}
            deferred_inputs = {}
            
            deps_fut = asyncio.Future()
            self._sub_resource_deps_futures[res_name] = deps_fut

            for key in keys:
                val_fut = asyncio.Future()
                res_futs[key] = val_fut
                
                known_fut = asyncio.Future()
                known_fut.set_result(True)
                secret_fut = asyncio.Future()
                secret_fut.set_result(False)
                
                deferred_inputs[key] = Output(deps_fut, val_fut, known_fut, secret_fut)
            
            sub_resource_futures[res_name] = res_futs

            protect = getattr(setup_method, "_protect", False)
            dep_names = depends_on_map.get(res_name, [])
            deps = [getattr(self, dep) for dep in dep_names if hasattr(self, dep)]
            
            child_opts = pulumi.ResourceOptions(parent=self, protect=protect, depends_on=deps)
            
            child_instance = child_class(f"{name}-{res_name}", **deferred_inputs, opts=child_opts)
            setattr(self, res_name, child_instance)

        # 5. Launch all setup methods
        for res_name in sorted_res_names:
            setup_method = setup_methods[res_name]
            futs = sub_resource_futures[res_name]
            asyncio.create_task(self._run_async_setup(res_name, setup_method, futs))

        # 6. Legacy setup handling (for backwards compatibility)
        if self.__class__.setup != Component.setup:
            futures = {}
            for output_name in self._get_outputs():
                output, futures[output_name] = from_nothing()
                setattr(self, output_name, output)

            if asyncio.iscoroutinefunction(self.setup):
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
        fields = []
        for field_name, field_type in get_type_hints(cls).items():
            if field_name.startswith('_'):
                continue
            if issubclass(field_type, pulumi.Output):
                fields.append(field_name)
        return fields

    @task
    async def _inittask(self, futures, *pargs, **kwargs):
        try:
            outs = await unwrap(self.setup(*pargs, **kwargs))
        except Exception as e:
            for f in futures.values():
                f.set_exception(e)
            raise
        else:
            self._process_outs(outs, futures)

    def _process_outs(self, outs: Optional[pulumi.Inputs], futures=None):
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

    async def _run_async_setup(self, res_name: str, setup_method, futures_dict: Dict[str, asyncio.Future]):
        deps_set = set()
        token = current_task_dependencies.set(deps_set)
        
        try:
            is_generator = inspect.isasyncgenfunction(setup_method)
            
            if is_generator:
                try:
                    async for val in setup_method():
                        if isinstance(val, dict):
                            for k, v in val.items():
                                if k in futures_dict and not futures_dict[k].done():
                                    futures_dict[k].set_result(v)
                except UnknownValueException:
                    pass
            else:
                if inspect.iscoroutinefunction(setup_method):
                    try:
                        val = await setup_method()
                        if isinstance(val, dict):
                            for k, v in val.items():
                                if k in futures_dict and not futures_dict[k].done():
                                    futures_dict[k].set_result(v)
                    except UnknownValueException:
                        pass
                else:
                    try:
                        val = setup_method()
                        if isinstance(val, dict):
                            for k, v in val.items():
                                if k in futures_dict and not futures_dict[k].done():
                                    futures_dict[k].set_result(v)
                    except UnknownValueException:
                        pass

            # Resolve remaining keys to UNKNOWN
            for k, fut in futures_dict.items():
                if not fut.done():
                    fut.set_result(UNKNOWN)

        except Exception as e:
            for fut in futures_dict.values():
                if not fut.done():
                    fut.set_exception(e)
            raise
        finally:
            current_task_dependencies.reset(token)
            if not self._sub_resource_deps_futures[res_name].done():
                self._sub_resource_deps_futures[res_name].set_result(deps_set)
