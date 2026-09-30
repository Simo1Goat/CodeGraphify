# Slide content: graph output comparison

Everything needed for the three-panel slide. Subject: **eShopOnWeb**, Microsoft's
ASP.NET reference application (10 projects, 254 C# files) — a real codebase, not a
test fixture, so the numbers mean something to a client.

Both engines analysed the same repository at the same commit.

---

## Panel 1 (left) — tree-sitter

### Screenshot

Open **<http://localhost:7475>** (login `neo4j` / `jawdahGraph123`) and run:

```cypher
MATCH (c {repo_id:'eshoponweb', name:'BasketService'})-[:CONTAINS]->(m)
OPTIONAL MATCH (m)-[call:CALLS]->(t)
RETURN c, m, call, t
```

Shows one real business class and everything it calls. ~20 relationships — readable
at slide size.

### KPIs

| | |
|---|---|
| **Nodes** | **2,378** |
| **Connections** | **4,881** |
| Call relationships | 1,519 |
| …that reach an actual method | **9  (0.6%)** |
| Unresolved placeholder nodes | 748 (31% of all nodes) |

### Red sentence (the limitation)

> Tree-sitter records the *text* of a call — `_basketRepository.UpdateAsync` — but
> never links it to the method being called: 99.4% of its call relationships end at a
> placeholder, so the graph cannot answer "what breaks if I change this method?"

---

## Panel 2 (middle) — Roslyn

### Screenshot

Open **<http://localhost:7474>** (login `neo4j` / `codegraphify`) and run:

```cypher
MATCH (c:Class {name:'BasketService'})-[:DECLARES]->(m)
OPTIONAL MATCH (m)-[call]->(t) WHERE type(call) STARTS WITH 'CALLS'
RETURN c, m, call, t
```

Same class, same code, so the two screenshots are directly comparable.

### KPIs

| | |
|---|---|
| **Nodes** | **3,737** |
| **Connections** | **15,523** |
| Call relationships | 6,951 |
| …that reach an actual method | **6,951  (100%)** |
| — inside the project | 544 |
| — into libraries, fully identified | 6,407 |

### The contrast to point at

Same line of code, in `BasketService`:

| tree-sitter records | Roslyn records |
|---|---|
| `'_basketRepository.UpdateAsync'` | `Ardalis.Specification.IRepositoryBase<T>.UpdateAsync` |
| `'basket.AddItem'` | `...BasketAggregate.Basket.AddItem` |
| type: *placeholder* | type: *method, with signature and assembly* |

Both graphs contain nodes labelled "External" — **but they mean opposite things.**
Roslyn's are resolved library methods with a full signature and assembly name.
tree-sitter's are unresolved text it could not connect to anything. Worth saying out
loud if anyone reads the labels.

---

## Panel 3 (right) — 3 bullets

**1. The gap is structural, not a matter of tuning.**
In C#, deciding which `Accept` a call reaches requires knowing the *type* of the
receiver — and that requires compiling the code. Roslyn does it because Roslyn *is*
the compiler. No amount of tuning closes this for a text-based parser.

**2. tree-sitter still wins outside .NET — and does one thing Roslyn cannot.**
21 languages against Roslyn's one, plus dependency-injection analysis (service
registrations, lifetimes, keyed services) that Roslyn does not model at all. It found
all of it correctly.

**3. Way forward: Roslyn for .NET, tree-sitter for the rest — one graph.**
Both write the same graph contract into Neo4j, so they are complements, not a
replacement. Roslyn becomes the C# engine; tree-sitter stays the fallback and covers
every other language.

---

## If the numbers are challenged

Have these ready — a sharp client will ask.

**"Why does Roslyn have 3× the connections?"**
Two reasons, both legitimate. It resolves every call to a named target, and it runs
the Razor/Blazor source generators, so it also sees the code generated from `.cshtml`
and `.razor` files — which tree-sitter cannot see at all. Excluding generated views,
Roslyn still finds **2,935** call relationships against tree-sitter's 1,519.

**"Is 0.6% really fair?"**
Yes, and it is worth being precise about *why*: tree-sitter is not producing *wrong*
answers, it is producing *no* answer. On the controlled test, every target it did
commit to was correct. The practical consequence is the same — the impact question
cannot be answered — but it is an honest distinction and it protects the claim.

**"Does this mean tree-sitter is bad?"**
No. It is doing what a syntax parser can do, across 21 languages, quickly. The
finding is narrower: for a C# *call graph* it cannot resolve targets, and the call
graph is the part that answers the question people actually ask of a dependency
graph.

---

## Reproducing the numbers

```bash
cd ~/PycharmProjects/CodeGraphify
.venv/bin/python - <<'EOF'
from neo4j import GraphDatabase
from benchmark.config import ROSLYN as R, TREE_SITTER as T
scan = "5e9fdd85b6f1494890b4ba32541b180d"

d = GraphDatabase.driver(R.uri, auth=(R.user, R.password))
with d.session() as s:
    print("roslyn nodes", s.run("MATCH (n:CodeEntity {last_seen_scan_id:$s}) RETURN count(n) AS c", s=scan).single()["c"])
    print("roslyn rels ", s.run("MATCH ()-[x]->() WHERE x.last_seen_scan_id=$s RETURN count(x) AS c", s=scan).single()["c"])
d.close()

d = GraphDatabase.driver(T.uri, auth=(T.user, T.password))
with d.session() as s:
    print("tree-sitter nodes", s.run("MATCH (n {repo_id:'eshoponweb'}) RETURN count(n) AS c").single()["c"])
    print("tree-sitter rels ", s.run("MATCH (a {repo_id:'eshoponweb'})-[x]->(b {repo_id:'eshoponweb'}) RETURN count(x) AS c").single()["c"])
    tot = s.run("MATCH (a {repo_id:'eshoponweb'})-[c:CALLS]->() RETURN count(c) AS c").single()["c"]
    ext = s.run("MATCH (a {repo_id:'eshoponweb'})-[c:CALLS]->(b:External) RETURN count(c) AS c").single()["c"]
    print(f"tree-sitter calls reaching a real method: {tot-ext} of {tot}")
d.close()
EOF
```
