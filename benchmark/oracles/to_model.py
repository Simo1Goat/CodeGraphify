"""Turn the golden file into the same Decl records the engines are read into.

Scored in the engine-neutral model so no engine's vocabulary is privileged.
"""

from __future__ import annotations

from benchmark.model import Decl, KindClass

KIND_CLASS = {
    "Namespace": KindClass.NAMESPACE,
    "Class": KindClass.TYPE,
    "Record": KindClass.TYPE,
    "Struct": KindClass.TYPE,
    "Interface": KindClass.TYPE,
    "Enum": KindClass.TYPE,
    "Delegate": KindClass.DELEGATE,
    "Method": KindClass.CALLABLE,
    "Constructor": KindClass.CALLABLE,
    "Property": KindClass.PROPERTY,
    "Field": KindClass.FIELD,
    "Event": KindClass.EVENT,
}


def declarations(golden) -> list[Decl]:
    decls = []
    for row in golden.declarations:
        kind = row["kind"]
        decls.append(Decl(
            engine="golden",
            kind_raw=kind,
            kind_class=KIND_CLASS.get(kind, KindClass.OTHER),
            name=row["name"],
            path=row["path"],
            start_line=row.get("start_line"),
            owner=row.get("owner"),
            arity=row.get("arity"),
            raw={"uid": row["id"], "q": row.get("q", [])},
        ))
    return decls
