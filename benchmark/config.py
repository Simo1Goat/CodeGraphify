"""Where everything lives, and the tolerances the run is judged by.

Both engines write the default Neo4j database `neo4j` on their own instance, so
isolation is by PORT, not by database name. Getting these two swapped would make
the harness compare a graph with itself and report perfect agreement, which is why
gate G4 checks them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class EngineEndpoint:
    name: str
    uri: str
    user: str
    password: str
    database: str = "neo4j"


#: Engine R — the Roslyn extractor in this repository.
ROSLYN = EngineEndpoint("roslyn", "bolt://localhost:7687", "neo4j", "codegraphify")

#: Engine T — the tree-sitter service in /home/mhabibi/PycharmProjects/code-graph.
#: Deliberately non-default ports; see its docker-compose.yml.
TREE_SITTER = EngineEndpoint("tree-sitter", "bolt://localhost:7688", "neo4j", "jawdahGraph123")

TREE_SITTER_API = "http://localhost:8090"


@dataclass(frozen=True)
class Corpus:
    name: str
    root: Path
    solution: Path
    golden: Path
    #: How the tree-sitter container sees this directory (it runs in Docker).
    container_path: str


NEUTRAL = Corpus(
    name="neutral",
    root=REPO_ROOT / "benchmark" / "corpora" / "NeutralGraphQuestions",
    solution=REPO_ROOT / "benchmark" / "corpora" / "NeutralGraphQuestions" / "NeutralGraphQuestions.sln",
    golden=REPO_ROOT / "benchmark" / "corpora" / "golden" / "neutral.golden.yaml",
    container_path="/workspace/eShopOnWeb",
)

CORPORA = {NEUTRAL.name: NEUTRAL}

#: Declaration start lines should agree exactly between engines. Pairings further
#: apart than this are refused during the join, and a low exact-agreement rate
#: fails gate G6 rather than quietly producing noise.
LINE_TOLERANCE = 2
MIN_EXACT_LINE_AGREEMENT = 0.90

ARTIFACTS = REPO_ROOT / "benchmark" / "artifacts"
