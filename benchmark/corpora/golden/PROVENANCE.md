# Provenance of the golden file

The corpus and its golden file were written before either engine was run on them.
This log records every change made **after** seeing a benchmark result, so a
reader can tell which facts were authored blind and which were not.

A golden quietly edited to agree with an engine is worthless. The rule here is
that such edits are allowed only when the source itself proves the golden wrong,
and each one is written down with its reason.

## Changes after first scoring

**1. Implied parent namespaces — no golden change; scoring changed instead.**

Roslyn emits `Neutral` and `Neutral.Domain` as namespace nodes. No source line
declares them; they exist only as prefixes of `Neutral.Domain.Orders` and the
rest. Tree-sitter emits only namespaces that are written.

Both are defensible. Rather than add them to the golden — which would have
penalised tree-sitter for a modelling choice — implied parents are now dropped
from both engines' namespace sets, and "models the namespace tree" is reported in
the capability table.

Neither engine gained or lost a point from a fact invented after the fact.

**2. Partial types — join changed, golden unchanged.**

`OrderValidator` is one type declared in two files. Roslyn emits one node
recording both sites; tree-sitter emits one node per file. Joining types by file
charged tree-sitter a false positive for the second node.

Types are now joined on their qualified name rather than their file, so a partial
type is one entity on every side. How each engine represents it is reported
separately.

## Changes to the corpus after first scoring

None.
