"""Canonicalising declarations so the join compares like with like.

Each source names the same construct differently. Left alone, those differences
are scored as errors, which measures the harness rather than the engines. Three
normalizations are needed, and each is a convention choice that must not favour
one side:

1. **Constructors.** Roslyn calls them `.ctor`; tree-sitter and the golden name
   them after the owning type. Canonical form is the type's simple name, which is
   what the source actually says.
2. **Explicit interface implementations.** Roslyn names them
   `IOrderSink.Accept`, tree-sitter just `Accept`. The golden followed Roslyn's
   convention, which would have handed Roslyn free matches — so the qualifier is
   stripped on all sides, and recording explicit-ness becomes a separate question
   rather than a naming advantage.
3. **Namespaces.** Roslyn emits one global node per namespace with no file;
   tree-sitter emits one per file. Neither is wrong, so they are never joined by
   path — they are compared as name sets, elsewhere.
"""

from __future__ import annotations

from dataclasses import replace

from benchmark.model import Decl, KindClass


def canonical(decls: list[Decl]) -> list[Decl]:
    return [_canonical_one(d) for d in decls]


def _canonical_one(decl: Decl) -> Decl:
    name = decl.name

    # Strip an explicit-interface qualifier: `IOrderSink.Accept` -> `Accept`.
    # Only for members; a namespace's dotted name is its identity.
    if decl.kind_class in {KindClass.CALLABLE, KindClass.PROPERTY, KindClass.EVENT} and "." in name:
        name = name.rsplit(".", 1)[-1]

    # Constructors: canonical name is the owning type's simple name.
    if decl.kind_raw == "Constructor" or name == ".ctor":
        owner = (decl.owner or "").rsplit(".", 1)[-1]
        if owner:
            name = owner

    return replace(decl, name=name) if name != decl.name else decl


def namespace_names(decls: list[Decl], declared: set[str] | None = None) -> set[str]:
    """The set of namespaces a source declares, compared as names only.

    Roslyn holds one global node per namespace (no file, no line); tree-sitter one
    per file. Comparing those per path would score a representation difference as
    an accuracy failure.

    Roslyn also models the namespace TREE, emitting implied parents (`Neutral`,
    `Neutral.Domain`) that no source line declares; tree-sitter emits only what is
    written. Both are defensible, so when the declared set is supplied, implied
    parents are dropped from both sides and the difference is reported as a
    capability rather than scored as precision.
    """
    names = {d.name for d in decls if d.kind_class is KindClass.NAMESPACE and d.name}
    if declared is None:
        return names
    return {
        name for name in names
        if name in declared
        or not any(other.startswith(name + ".") for other in declared)
    }


def implied_namespaces(decls: list[Decl], declared: set[str]) -> set[str]:
    """Parent namespaces an engine emits that no source line declares."""
    names = {d.name for d in decls if d.kind_class is KindClass.NAMESPACE and d.name}
    return {
        name for name in names
        if name not in declared and any(other.startswith(name + ".") for other in declared)
    }
