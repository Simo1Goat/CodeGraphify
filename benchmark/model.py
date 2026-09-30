"""The engine-neutral comparison model.

Neither engine's schema is used as the comparison vocabulary: one has no
fully-qualified name, the other has no line-based key, and scoring in either
engine's own terms would privilege it. Both are normalized into these records
first, and the golden is loaded into the same shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class KindClass(StrEnum):
    """Comparable declaration kinds.

    A kind an engine does not model at all is a capability gap, reported
    separately and excluded from that engine's recall denominator — otherwise
    "scored 0" reads as an error rather than an absent feature.
    """

    NAMESPACE = "Namespace"
    TYPE = "Type"          # Class, Record, Struct, Interface, Enum
    DELEGATE = "Delegate"
    CALLABLE = "Callable"  # Method, Constructor, LocalFunction
    PROPERTY = "Property"
    FIELD = "Field"
    EVENT = "Event"
    FILE = "File"
    EXTERNAL = "External"
    OTHER = "Other"


class EdgeClass(StrEnum):
    CONTAINMENT = "Containment"
    HIERARCHY = "Hierarchy"        # INHERITS / IMPLEMENTS
    MEMBER_MAP = "MemberMap"       # OVERRIDES / IMPLEMENTS_MEMBER
    CALL = "Call"
    CREATE = "Create"
    PROJECT_REF = "ProjectRef"
    DI = "DependencyInjection"
    OTHER = "Other"


@dataclass(frozen=True)
class Decl:
    """One declaration, as some engine reported it."""

    engine: str
    kind_raw: str
    kind_class: KindClass
    name: str
    path: str
    start_line: int | None
    owner: str | None = None          # engine's notion of the declaring type
    arity: int | None = None
    external: bool = False
    generated: bool = False
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def join_key(self) -> tuple[str, KindClass, str]:
        """Deliberately excludes the line number.

        Putting the line in the key means one systematic off-by-one convention
        difference destroys the whole join and is reported as ~0% agreement.
        Collisions are resolved by assignment on line distance instead, which
        measures the offset rather than assuming it.
        """
        if self.kind_class is KindClass.TYPE:
            # A type's identity is its qualified name, not its file. Joining types
            # by path would split a PARTIAL type across its declaration sites and
            # charge whichever engine models it as one node with a false negative
            # (or the other with a false positive) for a representation choice.
            return ("", self.kind_class, f"{self.owner or ''}.{self.name}")
        return (self.path, self.kind_class, self.name)


@dataclass(frozen=True)
class Edge:
    """One relationship, as some engine reported it."""

    engine: str
    edge_class: EdgeClass
    rel_raw: str
    src_uid: str
    dst_uid: str | None = None          # engine-local id of the target, if resolved
    dst_name: str | None = None         # the literal target text / simple name
    dst_external: str | None = None     # external symbol name, when known
    path: str | None = None
    line: int | None = None
    dispatch: str | None = None
    resolution: str | None = None
    confident: bool = True
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass
class EngineGraph:
    engine: str
    corpus: str
    decls: list[Decl] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    reported_counts: dict[str, int] = field(default_factory=dict)

    def by_uid(self) -> dict[str, Decl]:
        raise NotImplementedError  # engines supply their own uid mapping
