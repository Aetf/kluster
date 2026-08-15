from pathlib import Path
import fnmatch
import re
from typing import Any, List, Sequence

from jinja2 import Environment, PackageLoader
import pulumi
import pulumi_kubernetes as k8s
import pulumi_crds as crds

from putils import Component

from .config import versions


class Node(k8s.core.v1.NodePatch):
    @property
    def hostname_selector(self) -> k8s.core.v1.NodeSelectorTermArgs:
        return k8s.core.v1.NodeSelectorTermArgs(
            match_expressions=[
                k8s.core.v1.NodeSelectorRequirementArgs(
                    key='kubernetes.io/hostname', operator='In', values=[self.metadata.name]
                )
            ]
        )


class NamespaceProbe(Component, pulumi_type='kluster:utils:NamespaceProbe'):
    namespace: pulumi.Output[str]

    def __init__(self, name: str, opts: pulumi.ResourceOptions | None = None):
        super().__init__(name, opts=opts)
        cm = k8s.core.v1.ConfigMap(
            name,
            data={
                'comment': 'This is a workaround for pulumi not able to get namespace from the provider',
            },
            opts=self.child_opts(),
        )
        self.namespace = cm.metadata.namespace
        self.register_outputs({'namespace': self.namespace})


class HelmChart(k8s.helm.v4.Chart):
    def __init__(
        self,
        name: str,
        chart: str,
        namespace: pulumi.Input[str],
        *,
        version: str | None = None,
        repository_opts: k8s.helm.v4.RepositoryOptsArgs | None = None,
        opts: pulumi.ResourceOptions | None = None,
        **kwargs,
    ):
        if repository_opts is None:
            repository_opts = k8s.helm.v4.RepositoryOptsArgs()
        repository_opts.repo = versions.chart[chart].repo

        if version is None:
            version = versions.chart[chart].version

        def chart_naming_workaround(args: pulumi.ResourceTransformationArgs):
            return pulumi.ResourceTransformationResult(
                props=args.props,
                opts=pulumi.ResourceOptions.merge(args.opts, pulumi.ResourceOptions(delete_before_replace=True)),
            )

        opts = pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(transformations=[chart_naming_workaround]))

        super().__init__(
            name,
            version=version,
            chart=chart,
            namespace=namespace,
            repository_opts=repository_opts,
            opts=opts,
            **kwargs,
        )

    def service(self, name_pattern: str | None = None) -> pulumi.Output[k8s.core.v1.Service]:
        def to_urns(resources: Sequence[Any] | None = None) -> pulumi.Output[List[tuple[pulumi.Resource, str]]]:
            if resources is None:
                return pulumi.Output.all([])

            return pulumi.Output.all([(res, res.urn) for res in resources if isinstance(res, pulumi.Resource)])

        def to_service(urns: List[tuple[pulumi.Resource, str]]) -> pulumi.Output[k8s.core.v1.Service]:
            if len(urns) == 0:
                raise ValueError('No service found in the chart')
            if name_pattern is None:
                if len(urns) == 1:
                    assert isinstance(urns[0][0], k8s.core.v1.Service)
                    return pulumi.Output.from_input(urns[0][0])
                else:
                    raise ValueError('Multiple services defined in the chart, specify a selector')
            for res, urn in urns:
                if re.search(name_pattern, urn) is not None:
                    assert isinstance(res, k8s.core.v1.Service)
                    return pulumi.Output.from_input(res)
            raise ValueError(f'No service found in the chart with matching name: {",".join(urn for _, urn in urns)}')

        return self.resources.apply(to_urns).apply(to_service)


class ConfigMap(k8s.core.v1.ConfigMap):
    def __init__(
        self,
        name: str,
        package: str,
        *data_globs: pulumi.Input[str],
        strip_components: pulumi.Input[int | None] | None = None,
        tpl_variables: pulumi.Inputs | None = None,
        **kwargs,
    ):
        env = Environment(loader=PackageLoader(package, 'static'))

        def inner(g: Sequence[str], sc: int | None, tv: dict[str, Any] | None) -> dict[str, str]:
            return self._render_templates(env, g, sc or 0, tv or {})

        data = pulumi.Output.all(g=data_globs, sc=strip_components, tv=tpl_variables).apply(
            # this way we kind of dodge the type checking
            lambda kwargs: inner(**kwargs)
        )
        super().__init__(name, data=data, **kwargs)

    def _render_templates(
        self, env: Environment, glob: Sequence[str], strip_components: int, tplVariables: dict[str, Any]
    ) -> dict[str, str]:
        def path_strip_components(path: Path, strip_components: int) -> str:
            return str(Path(*path.parts[strip_components:]))

        names = env.list_templates()
        # for each pattern, it creates a list of names, which are collected and flattened
        names = [name for pat in glob for name in fnmatch.filter(names, pat)]
        return {
            path_strip_components(Path(name), strip_components): env.get_template(name).render(tplVariables)
            for name in names
        }


class SealedSecret(crds.bitnami.v1alpha1.SealedSecret):
    _name: str

    def __init__(
        self,
        name: str,
        encrypted_data: pulumi.Inputs,
        template: crds.bitnami.v1alpha1.SealedSecretSpecTemplateArgs | None = None,
        metadata: k8s.meta.v1.ObjectMetaArgs | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ):
        # Workaround https://github.com/pulumi/crd2pulumi/issues/26
        self._name = name

        metadata = k8s.meta.v1.ObjectMetaArgs(
            **{
                **metadata.__dict__,
                'name': name,
                'annotations': {
                    'sealedsecrets.bitnami.com/namespace-wide': 'true',
                },
            }
        )

        template = template or crds.bitnami.v1alpha1.SealedSecretSpecTemplateArgs()
        spec = crds.bitnami.v1alpha1.SealedSecretSpecArgs(
            encrypted_data=encrypted_data,
            template=crds.bitnami.v1alpha1.SealedSecretSpecTemplateArgs(
                **{
                    **template.__dict__,
                    'metadata': k8s.meta.v1.ObjectMetaArgs(
                        **{
                            **(template.metadata or {}).__dict__,
                            'annotations': {
                                'sealedsecrets.bitnami.com/namespace-wide': 'true',
                            },
                        }
                    ),
                }
            ),
        )

        # Delete before replace because name is fixed
        opts = opts or pulumi.ResourceOptions()
        opts.delete_before_replace = True

        super().__init__(name, metadata=metadata, spec=spec, opts=opts)

    def mount(self, dest: pulumi.Input[str], src: pulumi.Input[str] | None = None):
        raise NotImplementedError('mount')

    def as_secret_ref(self) -> pulumi.Output[k8s.core.v1.SecretEnvSourceArgs]:
        return pulumi.Output.from_input(k8s.core.v1.SecretEnvSourceArgs(name=self._name))

    # TODO: add other helpers
