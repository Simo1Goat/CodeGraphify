# Benchmark: tree-sitter vs Roslyn for .NET dependency graphs

## The question

Which parser should we rely on to build a dependency graph for a .NET codebase?

Two implementations exist:

| | Engine | Scope | Neo4j |
|---|---|---|---|
| **T** | tree-sitter (`/PycharmProjects/code-graph`) | syntax-only, 21 languages | bolt **7688** |
| **R** | Roslyn (`/PycharmProjects/CodeGraphify`) | semantic, C# only | bolt **7687** |

## Why not just compare the two graphs to each other

That would measure *agreement*, not correctness — and it could never reveal a mistake
in R, which we wrote. A missing edge is also only detectable against something that
knows it should be there.

So both engines are scored against **an independent reference**: a purpose-built C#
corpus whose every correct answer is written down by hand from the source. The golden
file was not generated from either engine's output, and must never be — a golden
derived from an engine measures that engine against itself.

## The corpus

`corpora/NeutralGraphQuestions/` — 404 lines of C# across 3 projects
(`Domain`, `Services`, `Host`), so cross-project resolution is exercised.

Written from **the questions a dependency graph must answer**, before consulting
either engine's feature list. Scoring on a fixture built to show off one engine's
features would decide the result by construction.

Deliberately balanced:

- favours T: DI registrations (3 lifetimes + keyed + cross-file composition), project
  and package references
- favours R: two overload sets, a two-level override chain, explicit interface
  implementations, fields/events/delegates
- **constructs each engine is expected to fail**: 6 receiver-less calls, a
  `this.`-qualified call, two types sharing a simple name across namespaces

## Ground truth

`corpora/golden/neutral.golden.yaml` — 104 declarations, 41 calls, 15 object
creations, 8 hierarchy edges, 15 member mappings, 4 DI registrations, 6 negative
assertions.

Kept honest by `oracles/golden.py`, which fails the run rather than silently
mis-scoring:

- all 145 `evidence.quote` strings are re-read from the source at their recorded
  lines, so fixture drift breaks the build
- `counts` must equal the number of rows, so a fact cannot be quietly deleted
- every referenced id must resolve
- `expected_absent` names the specific wrong answers each engine's design invites,
  which is what gives a 400-line corpus real precision denominators

`corpora/golden/PROVENANCE.md` logs every change made *after* seeing a result.

## What is measured

**Calls are decomposed into levels rather than collapsed into one F1**, because a
single number would hide the thing being measured:

| Level | Question |
|---|---|
| L0 | Did it find that this member calls anything? |
| L2 | Is the final identifier of the target right? |
| L3a | Does it reach the right method on the right **type**? |
| L3b | Does it reach the right **overload**? |

`L3a − L3b` is the overload story; `L2 − L3a` is the namespace/receiver story.

All call metrics are per **(caller, callee) pair**, not per call site: T de-duplicates
edges in its builder, so counting sites would penalise a modelling choice rather than
an accuracy failure. T's C# call edges also carry no line number, so call-*site*
detection is not measurable for it at all — that absence is itself a result.

Also measured: declaration precision/recall per kind, owner correctness, overload
discrimination **against a random-pick baseline**, and precision of confident claims
(so honestly recording uncertainty is neither rewarded nor punished).

**Capability is held strictly apart from accuracy.** A kind an engine does not model
at all is removed from its denominator and reported separately — otherwise
"tree-sitter does not model fields" reads as "tree-sitter got the fields wrong".

## Results

Recall against the golden, with Wilson 95% intervals in the harness output:

| | Roslyn | tree-sitter |
|---|---|---|
| declarations F1 | **100.0%** | 96.9% |
| owner (right declaring type) | 100.0% | 100.0% |
| L0 — found the caller | 100.0% | 89.5% |
| L2 — right target *name* | 87.5% | 77.5% |
| **L3a — right type + name** | **100.0%** | **4.0%** |
| **L3b — right overload** | **100.0%** | **4.0%** |
| overload discrimination | 100% (chance 50%) | 0% (chance 50%) |
| precision when it *does* resolve | 100% (25/25) | 100% (1/1) |

Capability gaps excluded from T's denominator: `Field` (11), `Delegate` (1),
`Event` (1) — it models none of these for C#.

### Reading these

**tree-sitter finds the call and usually the name, but reaches the actual declaration
for 1 of 25 internal calls.** Its call target is the literal receiver text
(`_repo.GetAsync`) matched against a flat index of bare names, so it structurally
cannot reach a declaration; its one success is a DI extension method. Its own tests
enshrine this behaviour.

Its precision is 100% when it does resolve — it is not producing *wrong* answers, it
is producing *no* answer. For "what breaks if I change this method" the outcome is
the same; for trusting what it does say, it is meaningfully better than being wrong.

Where T holds up: declarations (96.9% F1), namespace qualification (100%), and its DI
layer found all 4 registrations with the correct lifetimes and the keyed one — a
capability R has nothing equivalent to.

## Five harness bugs found before any number was believed

Each would have misreported an engine. They are listed because they are the reason to
trust the rest:

1. Namespaces joined by file path — R emits one *global* node, T one *per file*.
   Scored R at 0% until compared as name sets.
2. R's implied parent namespaces (`Neutral`, `Neutral.Domain`) counted as false
   positives. Neither engine is wrong; dropped from both sides.
3. Constructors: `.ctor` in R, type-name in T and the golden.
4. **Explicit interface implementations**: `IOrderSink.Accept` in R, `Accept` in T —
   and the golden had followed R's convention, *handing R free matches*. That was
   bias in our own ground truth.
5. `last_segment` split generics at the first `<`, turning `List<T>.Add` into `List`
   and scoring every generic call as a miss.

A monotonicity assertion (`TP(L3b) ⊆ TP(L3a) ⊆ TP(L2)`) caught a sixth: L3a reported
*fewer* true positives than L3b, because keying on less information merged the two
overloads into one entry.

## Running it

Both engines must have analyzed the corpus first:

```bash
# Roslyn
cd ~/PycharmProjects/CodeGraphify && source scripts/dotnet-env.sh
.venv/bin/python -m app.scan --input benchmark/corpora/NeutralGraphQuestions/NeutralGraphQuestions.sln \
  --repo-root benchmark/corpora/NeutralGraphQuestions --repo-id neutral --scan-id neutral-r-001

# tree-sitter (needs the corpus mounted; see note below)
curl -X POST http://localhost:8090/v1/local/analyze \
  -H 'Content-Type: application/json' -d '{"path":"/workspace/eShopOnWeb","repo_id":"neutral"}'
```

> **One change outside this repo.** `code-graph`'s container mounts only
> `${ESHOP_PATH}`, so `code-graph/.env` was pointed at the corpus. The original is
> saved at `code-graph/.env.benchmark-backup`. Nothing else in that repo was modified;
> it is imported read-only.

## What this does and does not show

**Does**

- Whether each engine's graph is *complete* for a .NET project, against a reference
  that is neither engine.
- Whether T's C# call edges can answer target-level questions at all — quantified per
  level, with the 14.6% receiver-less share published as its disclosed ceiling.
- Overload discrimination against chance, which is the sharpest single number here.
- T's genuine advantage (DI) and R's complete absence there.

**Does not**

- **Scale.** 400 lines cannot separate 97% from 99% — the intervals show it (R's L3b
  is 100% but `[87%–100%]`). Nothing here predicts behaviour on a large solution.
- **Any language other than C#.** T covers 20+; that is a real advantage this
  benchmark does not measure.
- Performance, memory, or incremental re-scan behaviour.
- **Authorship bias is bounded, not eliminated.** We wrote both the corpus and R.
  The bounds are: the corpus was written from questions rather than features, the
  golden is independent of both engines, `expected_absent` catches fabrication, and
  every post-hoc change is logged. R scoring 100% on a corpus we wrote is weaker
  evidence than R scoring 100% on someone else's.

## Status

Implemented: corpus, golden + verifying loader, both Neo4j readers, the join with
line calibration, declaration metrics, call metrics (L0/L2/L3a/L3b), overload
discrimination, uncertainty calibration, Wilson intervals.

Not yet: the independent text scanner (which cross-checks the golden's *completeness*,
the one control that would catch a construct we forgot to label), hierarchy and
member-map metrics, the DI metric, `expected_absent` checking, the capability matrix,
and a report renderer.
