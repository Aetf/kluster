"""The VictoriaMetrics operator's objects, declared untyped while the generated SDK for them cannot be imported.

**The defect.** `pulumi_crds.operator`, the package `sdks/crds` generates for
`operator.victoriametrics.com`, fails at import with `SyntaxError: duplicate
argument 'host_aliases' in function definition`. The operator's definitions
carry both `hostAliases` and the deprecated `host_aliases` on several kinds,
and the Python generator names both `host_aliases`, so every class holding
the pair declares one argument twice and no kind of the group can be
imported, `VMRule` included.

The defect is tracked as
[Aetf/kluster-ops#720](https://github.com/Aetf/kluster-ops/issues/720).

**The layer** is this module and nothing else: `operator_object` declares an
object of the group as a `pulumi_kubernetes.apiextensions.CustomResource`,
whose spec is the definition's own JSON, spelled as the operator reads it. It
registers under `type_token(kind)`,
`kubernetes:operator.victoriametrics.com/v1beta1:<kind>`: the token the SDK's
class names as an alias of its own `crds:` one, as every class of that SDK
does, so a stack's state follows the object to the class with nothing added at
the call.

**Removal**: once `import pulumi_crds.operator.v1beta1` succeeds, each call
becomes the SDK class of its kind, its spec the class's `Args`, and this
module goes, with what reads it in `tests/test_monitoring.py`: the tripwire
that holds the import's failure and goes red the day it imports, the check
of each spec against its definition, which the class's `Args` then make, and
the lookups by `type_token`, which then read the class's token.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import pulumi
import pulumi_kubernetes as k8s

__all__ = ('API_VERSION', 'OperatorKind', 'operator_object', 'type_token')

#: The group and version every kind declared here is served at.
API_VERSION = 'operator.victoriametrics.com/v1beta1'

#: The kinds the monitoring component declares.
type OperatorKind = Literal['VMAlertmanagerConfig', 'VMPodScrape', 'VMRule']


def type_token(kind: OperatorKind) -> str:
    """The type an object of `kind` declared here registers under."""
    return f'kubernetes:{API_VERSION}:{kind}'


def operator_object(
    resource_name: str,
    *,
    kind: OperatorKind,
    metadata: k8s.meta.v1.ObjectMetaArgs,
    spec: Mapping[str, Any],
    opts: pulumi.ResourceOptions | None = None,
) -> k8s.apiextensions.CustomResource:
    """An object of the operator's group, its spec written as the definition spells it."""
    return k8s.apiextensions.CustomResource(
        resource_name,
        api_version=API_VERSION,
        kind=kind,
        metadata=metadata,
        spec=dict(spec),
        opts=opts,
    )
