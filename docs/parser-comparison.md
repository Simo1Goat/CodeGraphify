# Which parser should we use for .NET code?

A comparison of two ways of turning a C# codebase into a dependency graph.

---

## The two options

| | How it works | Covers |
|---|---|---|
| **tree-sitter** | Reads the *shape* of the code, like a very good text parser | 21 languages |
| **Roslyn** | Uses the actual C# compiler to understand the code | C# only |

tree-sitter is fast and works on almost any language. Roslyn only understands C#,
but it understands it the way the compiler does.

The question is whether that difference matters in practice for a .NET project.

---

## How we tested

We wrote a small C# project — about 400 lines, three sub-projects — and then wrote
down **by hand every correct answer**: every class, every method, and for every call
in the code, exactly which method it goes to.

That hand-written answer key is what both tools are graded against. We did not
compare the two tools to each other, because that would only tell us how much they
differ, not which one is right.

The test project was designed around *the questions a dependency graph needs to
answer*, before looking at what either tool happens to be good at. It deliberately
includes things we expected **each** tool to get wrong.

---

## What we measured

For every call in the code, we asked four questions — each harder than the last.

Take this one real line from the test project:

```csharp
_sink.Accept(order, force: true);
```

There are **two different methods called `Accept`** — one takes one argument, one
takes two. A useful graph has to know which one this is.

| | The question | Roslyn | tree-sitter |
|---|---|---|---|
| **1** | Did it notice a call happens here? | yes | yes |
| **2** | Did it get the method **name** right? (`Accept`) | yes | yes |
| **3** | Did it find the right **class** the method belongs to? | yes | **no** |
| **4** | Did it pick the right **version** of `Accept`? | yes | **no** |

Roslyn answers: *`IOrderSink.Accept(Order, bool)`* — the exact method.

tree-sitter answers: *`"_sink.Accept"`* — it records the text it saw, and stops
there. It does not connect that text to any actual method in the project.

---

## Results

Out of everything in the test project:

| | Roslyn | tree-sitter |
|---|---|---|
| Classes, methods and properties found | **96 of 96** | 78 of 83 |
| 1. Noticed the call | **19 of 19** | 17 of 19 |
| 2. Got the method name right | **35 of 40** | 31 of 40 |
| 3. Found the right class | **25 of 25** | **1 of 25** |
| 4. Picked the right version of the method | **25 of 25** | **1 of 25** |
| — where two versions existed | **5 of 5** | **0 of 5** |

*(tree-sitter's declaration score excludes fields, events and delegates, which it
does not attempt to read at all — counting those would be unfair.)*

### What this means in plain terms

**tree-sitter sees that a call happens and usually reads the name correctly, but it
connects the call to the actual method only 1 time in 25.**

So it can tell you *"something here calls something named `Accept`"*. It cannot
reliably tell you *"this calls `OrderService.Accept`, the two-argument one"*.

One important fairness point: when tree-sitter *does* commit to an answer, that
answer was correct every time. It is not giving wrong answers — it is giving **no**
answer. For a question like *"what breaks if I change this method?"* the practical
result is the same, but it does mean its output can be trusted as far as it goes.

### Where tree-sitter does well

- It finds classes, methods and properties accurately (94%).
- It correctly places them in the right namespace and class (100%).
- **It reads dependency-injection setup**, which Roslyn does not do at all — it found
  all 4 service registrations with the correct lifetimes.
- It handles 21 languages. Roslyn handles one.

---

## The recommendation

**For a .NET codebase, use Roslyn.**

The reason is narrow and specific: a dependency graph is mostly valuable for
answering *"what calls this, and what would break if I change it?"* — and that is
precisely the question tree-sitter cannot answer for C#, because it never connects a
call to the method it actually reaches.

This is not a flaw in tree-sitter. It reads code as text, and in C# you cannot tell
which `Accept` is meant without knowing the type of `_sink` — which requires
compiling the code. Roslyn does that because Roslyn *is* the compiler.

**tree-sitter remains the right tool for the other 20 languages**, and its
dependency-injection analysis is genuinely useful and has no equivalent on the
Roslyn side. The two are complements, not rivals — but for the C# call graph
specifically, Roslyn is the one that works.

---

## What this test does not prove

Stated plainly, because the numbers above look more decisive than a test this size
can justify:

- **It is a small test.** 400 lines. It shows clearly that one tool resolves calls
  and the other does not — a very large difference. It could not reliably tell you
  whether something is 97% or 99% accurate.
- **It only covers C#.** tree-sitter's support for 20 other languages is a real
  advantage that this comparison does not measure at all.
- **It does not measure speed, memory, or cost.**
- **We wrote both the test project and the Roslyn tool.** We limited the effect of
  that by designing the test around questions rather than features, by writing the
  answer key from the source code rather than from either tool's output, and by
  including cases we expected Roslyn to fail. It is a real constraint, not one we can
  claim to have eliminated.

The strongest evidence here is not the exact percentages. It is the size and the
nature of the gap on question 3: **1 out of 25 versus 25 out of 25** is a difference
in capability, not in tuning, and it follows directly from how each tool works.
