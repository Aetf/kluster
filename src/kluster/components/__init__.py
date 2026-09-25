"""Components: every reusable unit of resources, one package per area.

A component is anything a stack program declares rather than wires
(`docs/style/pulumi.md` under "Layering"), down to the leaf resources it owns.
An area is a package even when it holds one module, so that a path is guessable
from a name and a second module does not change its shape.

Areas may import each other — `homelab` names `talos`'s cluster type in its
signature — and may reach down to `providers`, `lib` and `conventions`. Nothing
here imports a stack program.
"""
