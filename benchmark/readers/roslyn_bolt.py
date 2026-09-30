"""Read Engine R's graph from its Neo4j.

Reads what was INGESTED, not the JSONL payload: the question is how accurate the
graph in the database is, so anything lost between extraction and ingest should
count against it.
"""

from __future__ import annotations

from neo4j import GraphDatabase

from benchmark.config import ROSLYN, EngineEndpoint
from benchmark.model import Decl, EdgeClass, EngineGraph, KindClass

#: Roslyn kind -> comparison class.
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
    "LocalFunction": KindClass.CALLABLE,
    "Property": KindClass.PROPERTY,
    "Field": KindClass.FIELD,
    "Event": KindClass.EVENT,
    "File": KindClass.FILE,
    "GeneratedFile": KindClass.FILE,
    "ExternalAssembly": KindClass.EXTERNAL,
    "ExternalType": KindClass.EXTERNAL,
    "ExternalMethod": KindClass.EXTERNAL,
    "UnresolvedReference": KindClass.OTHER,
}

EDGE_CLASS = {
    "CONTAINS": EdgeClass.CONTAINMENT,
    "DECLARES": EdgeClass.CONTAINMENT,
    "COMPILES": EdgeClass.CONTAINMENT,
    "COMPILES_AS": EdgeClass.CONTAINMENT,
    "GENERATES": EdgeClass.CONTAINMENT,
    "INHERITS": EdgeClass.HIERARCHY,
    "IMPLEMENTS": EdgeClass.HIERARCHY,
    "OVERRIDES": EdgeClass.MEMBER_MAP,
    "IMPLEMENTS_MEMBER": EdgeClass.MEMBER_MAP,
    "CALLS_DIRECT": EdgeClass.CALL,
    "CALLS_VIRTUAL": EdgeClass.CALL,
    "CALLS_INTERFACE": EdgeClass.CALL,
    "CALLS_UNRESOLVED": EdgeClass.CALL,
    "RESOLVES_TO": EdgeClass.CALL,
    "CREATES": EdgeClass.CREATE,
    "REFERENCES_PROJECT": EdgeClass.PROJECT_REF,
    "REFERENCES_ASSEMBLY": EdgeClass.PROJECT_REF,
    "DEPENDS_ON": EdgeClass.PROJECT_REF,
}

#: Engine R says outright how confident it is. `External` is confident: the target
#: is named, it simply lives outside the analyzed projects.
CONFIDENT_STATUSES = {"Resolved", "External"}


def read(scan_id: str, corpus: str, endpoint: EngineEndpoint = ROSLYN) -> EngineGraph:
    driver = GraphDatabase.driver(endpoint.uri, auth=(endpoint.user, endpoint.password))
    try:
        with driver.session(database=endpoint.database) as session:
            decls = _read_decls(session, scan_id)
            edges = _read_edges(session, scan_id)
            counts = _read_counts(session, scan_id)
    finally:
        driver.close()

    return EngineGraph(engine="roslyn", corpus=corpus, decls=decls, edges=edges,
                       reported_counts=counts)


def _read_decls(session, scan_id: str) -> list[Decl]:
    query = """
    MATCH (n:CodeEntity)
    WHERE n.last_seen_scan_id = $scan_id
    RETURN n.id AS uid, n.kind AS kind, n.name AS name, n.canonical_name AS canonical,
           n.file_path AS path, n.start_line AS start_line,
           n.declaring_type AS owner, n.parameter_count AS arity,
           n.external AS external, n.generated AS generated,
           n.namespace AS ns, n.synthesized AS synthesized, n.full_name AS full_name
    ORDER BY n.id
    """
    decls = []
    for record in session.run(query, scan_id=scan_id):
        kind = record["kind"]
        # A Namespace node's `name` is only its last segment; its dotted name is
        # in `full_name`, and the dotted name is its identity.
        name = record["full_name"] if kind == "Namespace" and record["full_name"] else record["name"]
        decls.append(Decl(
            engine="roslyn",
            kind_raw=kind,
            kind_class=KIND_CLASS.get(kind, KindClass.OTHER),
            name=name,
            path=record["path"] or "",
            start_line=record["start_line"],
            # For a type the owner is its namespace; for a member, its declaring type.
            owner=record["owner"] or record["ns"],
            arity=record["arity"],
            external=bool(record["external"]),
            generated=bool(record["generated"]) or bool(record["synthesized"]),
            raw={"uid": record["uid"], "canonical_name": record["canonical"]},
        ))
    return decls


def _read_edges(session, scan_id: str):
    from benchmark.model import Edge

    query = """
    MATCH (a:CodeEntity)-[r]->(b:CodeEntity)
    WHERE r.last_seen_scan_id = $scan_id
    RETURN a.id AS src, b.id AS dst, type(r) AS rel,
           b.name AS dst_name, b.canonical_name AS dst_canonical, b.external AS dst_external,
           r.file_path AS path, r.start_line AS line,
           r.dispatch_kind AS dispatch, r.resolution_status AS resolution
    ORDER BY a.id, type(r), b.id, r.start_line
    """
    edges = []
    for record in session.run(query, scan_id=scan_id):
        rel = record["rel"]
        resolution = record["resolution"]
        edges.append(Edge(
            engine="roslyn",
            edge_class=EDGE_CLASS.get(rel, EdgeClass.OTHER),
            rel_raw=rel,
            src_uid=record["src"],
            dst_uid=record["dst"],
            dst_name=record["dst_name"],
            dst_external=record["dst_canonical"] if record["dst_external"] else None,
            path=record["path"],
            line=record["line"],
            dispatch=record["dispatch"],
            resolution=resolution,
            confident=resolution in CONFIDENT_STATUSES if resolution else True,
        ))
    return edges


def _read_counts(session, scan_id: str) -> dict[str, int]:
    record = session.run(
        "MATCH (s:Scan {scan_id: $scan_id}) RETURN properties(s) AS props",
        scan_id=scan_id,
    ).single()
    if not record:
        return {}
    return {k: v for k, v in record["props"].items() if isinstance(v, int)}
