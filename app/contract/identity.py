"""Node identity policy. One place, because changing it invalidates stored ids.

The extractor computes these ids in C# (``Symbols/EntityId.cs``); this module is
the Python mirror used by the contract tests, so the two implementations cannot
drift silently.

Policy
------
A symbol declared in an analyzed project is keyed by its documentation comment
id **plus the compilation context that can change its meaning** (project and
target framework). The same ``OrderService.Submit`` compiled for ``net8.0`` and
for ``net48`` are therefore two nodes, because ``#if`` can give them different
bodies and different calls.

A symbol from outside the analyzed projects (a framework or NuGet type) is keyed
by **assembly plus documentation comment id, with no project context**, so every
project referencing ``System.String`` points at one shared node instead of one
copy per project. Those nodes are what makes "who uses this library" answerable.

Namespaces are keyed by name alone: a namespace is a naming scope, not a
compiled artifact, and a unified namespace tree is more useful than one copy per
project.

Files, projects and variants are keyed by repo-relative path, so they are stable
across scans regardless of where the repo is checked out.

Unresolved references have no symbol to name, so they are keyed by the call site
that produced them plus the text that could not be resolved.
"""

from __future__ import annotations

import hashlib

# Bump when the hashing scheme changes; it forces a full re-scan rather than
# silently mixing old and new ids in one graph.
IDENTITY_VERSION = "v1"

_DIGEST_CHARS = 32


def _hash(prefix: str, *parts: str | None) -> str:
    payload = "|".join([IDENTITY_VERSION, prefix, *(p or "" for p in parts)])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    return f"{prefix}:{digest}"


def repository_id(repo_id: str) -> str:
    return _hash("repo", repo_id)


def solution_id(solution_key: str) -> str:
    return _hash("sln", solution_key)


def project_id(project_key: str) -> str:
    return _hash("proj", project_key)


def variant_id(project_key: str, target_framework: str | None, configuration: str) -> str:
    return _hash("var", project_key, target_framework, configuration)


def file_id(repo_relative_path: str) -> str:
    return _hash("file", repo_relative_path)


def namespace_id(namespace_name: str) -> str:
    """Global namespace is the empty string."""
    return _hash("ns", namespace_name)


def symbol_id(canonical_name: str, project_key: str, target_framework: str | None) -> str:
    """A type or member declared in an analyzed project."""
    return _hash("sym", canonical_name, project_key, target_framework)


def external_symbol_id(canonical_name: str, assembly_name: str | None) -> str:
    """A type or member from a referenced assembly, shared across projects."""
    return _hash("ext", canonical_name, assembly_name)


def unresolved_id(
    file_path: str, start_line: int, start_column: int, expression_text: str
) -> str:
    return _hash("unres", file_path, str(start_line), str(start_column), expression_text)
