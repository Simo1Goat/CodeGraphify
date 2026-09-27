"""Answer "what kind of .NET codebase is this?" before trying to analyze it.

Reads .sln and .csproj files as XML. No MSBuild, no SDK, no restore, no Roslyn.
That is the point: this has to work on a codebase the extractor cannot open, on a
machine that could never open it, so the answer can be used to decide *where* to
run the extractor rather than discovered by watching it fail.

What it decides:

- **Project style.** SDK-style (``<Project Sdk="...">``) or legacy non-SDK
  (``ToolsVersion`` plus an explicit ``Microsoft.CSharp.targets`` import).
- **Target frameworks**, including the legacy ``TargetFrameworkVersion`` form
  (``v4.8``), normalized to moniker form (``net48``).
- **Whether Windows is required**, which is a property of the *project*, not of
  the project format: an SDK-style ``net8.0-windows`` WPF app needs Windows just
  as much as a non-SDK ``net48`` library does.
- **Whether restore has happened.** Unrestored projects are the most common cause
  of a degraded scan, and the extractor cannot tell the difference between "this
  package reference is missing" and "this code is broken".
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

# Directories never worth probing.
_SKIP_DIRECTORIES = {"bin", "obj", ".git", ".vs", "node_modules", "packages", ".idea"}


class ProjectStyle(StrEnum):
    SDK = "sdk"
    LEGACY = "legacy"
    UNKNOWN = "unknown"


class Verdict(StrEnum):
    """Where this codebase can be analyzed."""

    LINUX_OK = "linux-ok"
    NEEDS_WINDOWS = "needs-windows"
    MIXED = "mixed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProjectProbe:
    project_key: str
    style: ProjectStyle
    target_frameworks: tuple[str, ...]
    assembly_name: str | None
    output_type: str | None
    uses_packages_config: bool
    restored: bool
    windows_reasons: tuple[str, ...]
    parse_error: str | None = None
    target_framework_source: str | None = None
    """Which file the target framework was read from -- the project itself, or an
    imported directory-level props file. Worth reporting: when it is missing, this
    says where to look."""


    @property
    def needs_windows(self) -> bool:
        return bool(self.windows_reasons)


@dataclass
class RepoProbe:
    repo_root: str
    solutions: list[str] = field(default_factory=list)
    projects: list[ProjectProbe] = field(default_factory=list)
    verdict: Verdict = Verdict.UNKNOWN
    reasons: list[str] = field(default_factory=list)
    restore_needed: list[str] = field(default_factory=list)

    @property
    def windows_only_projects(self) -> list[ProjectProbe]:
        return [p for p in self.projects if p.needs_windows]

    def summary(self) -> str:
        """A few lines a human can act on."""
        lines = [
            f"repo: {self.repo_root}",
            f"verdict: {self.verdict}",
            (
                f"projects: {len(self.projects)} "
                f"({sum(1 for p in self.projects if p.style is ProjectStyle.SDK)} SDK-style, "
                f"{sum(1 for p in self.projects if p.style is ProjectStyle.LEGACY)} legacy)"
            ),
            f"solutions: {len(self.solutions)}",
        ]
        frameworks = sorted({tfm for p in self.projects for tfm in p.target_frameworks})
        if frameworks:
            lines.append(f"target frameworks: {', '.join(frameworks)}")
        for reason in self.reasons:
            lines.append(f"  - {reason}")
        if self.restore_needed:
            lines.append(
                f"restore needed for {len(self.restore_needed)} project(s); "
                "run 'dotnet restore' or scans will be partial-semantic"
            )
        return "\n".join(lines)


def probe_repository(repo_root: Path | str) -> RepoProbe:
    """Probe every project under ``repo_root``."""
    root = Path(repo_root).resolve()
    probe = RepoProbe(repo_root=str(root))

    if not root.is_dir():
        probe.reasons.append(f"{root} is not a directory")
        return probe

    for path in sorted(_walk(root, {".sln", ".slnx"})):
        probe.solutions.append(_relative(root, path))

    for path in sorted(_walk(root, {".csproj"})):
        probe.projects.append(probe_project(path, root))

    _decide(probe)
    return probe


def probe_project(project_path: Path, repo_root: Path) -> ProjectProbe:
    project_key = _relative(repo_root, project_path)

    try:
        # Local build files, not untrusted input.
        tree = ET.parse(project_path)
    except (ET.ParseError, OSError) as exc:
        return ProjectProbe(
            project_key=project_key,
            style=ProjectStyle.UNKNOWN,
            target_frameworks=(),
            assembly_name=None,
            output_type=None,
            uses_packages_config=False,
            restored=False,
            windows_reasons=(),
            parse_error=str(exc),
        )

    root = tree.getroot()
    style = _classify_style(root)

    own_properties = _read_properties(root)
    imported, imported_from = _imported_properties(project_path, repo_root)

    # The project's own values win over anything it imports.
    properties = {**imported, **own_properties}

    frameworks = _read_target_frameworks(properties)
    source: str | None = None
    if frameworks:
        source = "project" if _read_target_frameworks(own_properties) else imported_from

    return ProjectProbe(
        project_key=project_key,
        style=style,
        target_frameworks=frameworks,
        assembly_name=properties.get("assemblyname"),
        output_type=properties.get("outputtype"),
        uses_packages_config=(project_path.parent / "packages.config").exists(),
        restored=_is_restored(project_path, style),
        windows_reasons=_windows_reasons(properties, frameworks, style),
        target_framework_source=source,
    )


#: Files MSBuild and NuGet import automatically from a project's ancestor
#: directories. Modern repos put the target framework and package versions here
#: rather than in each project, so a probe that reads only .csproj files sees
#: nothing -- and then cannot tell a Windows-only repo from a portable one.
_AUTO_IMPORTED_FILES = (
    "Directory.Build.props",
    "Directory.Packages.props",
    "Directory.Build.targets",
)


def _imported_properties(
    project_path: Path, repo_root: Path
) -> tuple[dict[str, str], str | None]:
    """Properties from directory-level files, and which file they came from.

    Walks from the project's own directory up to the repository root, applying
    farther ancestors first so the nearest wins -- an approximation of MSBuild's
    evaluation, which is enough for a survey and needs no MSBuild. Explicit
    ``<Import>`` of a sibling .props file is followed too, one level deep.
    """
    properties: dict[str, str] = {}
    source: str | None = None

    directories: list[Path] = []
    current = project_path.parent.resolve()
    root = repo_root.resolve()
    while True:
        directories.append(current)
        if current == root or current.parent == current:
            break
        if root not in current.parents:
            break
        current = current.parent

    # Farthest ancestor first, so nearer definitions overwrite.
    for directory in reversed(directories):
        for name in _AUTO_IMPORTED_FILES:
            candidate = directory / name
            if not candidate.is_file():
                continue
            found = _read_properties_from_file(candidate)
            if found:
                properties.update(found)
                if "targetframework" in found or "targetframeworks" in found:
                    source = f"imported:{_relative(root, candidate)}"

    for imported in _explicit_imports(project_path):
        found = _read_properties_from_file(imported)
        if found:
            properties.update(found)
            if "targetframework" in found or "targetframeworks" in found:
                source = f"imported:{_relative(root, imported)}"

    return properties, source


def _explicit_imports(project_path: Path) -> list[Path]:
    """Relative .props/.targets files the project imports by path.

    Only relative paths that resolve to a real file: an import whose path is an
    unexpanded MSBuild property cannot be followed without evaluating MSBuild, which
    is the thing this module exists to avoid.
    """
    try:
        # Local build file, not untrusted input.
        tree = ET.parse(project_path)
    except (ET.ParseError, OSError):
        return []

    resolved: list[Path] = []
    for element in tree.getroot().iter():
        if _localname(element) != "import":
            continue
        raw = element.get("Project") or ""
        if not raw or "$(" in raw:
            continue
        candidate = (project_path.parent / raw.replace("\\", "/")).resolve()
        if candidate.is_file() and candidate.suffix.lower() in {".props", ".targets"}:
            resolved.append(candidate)
    return resolved


def _read_properties_from_file(path: Path) -> dict[str, str]:
    try:
        # Local build file, not untrusted input.
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        return {}
    return _read_properties(tree.getroot())


def _classify_style(root: ET.Element) -> ProjectStyle:
    # An SDK-style project declares its SDK, either as an attribute or as an
    # explicit <Sdk /> element.
    if root.get("Sdk"):
        return ProjectStyle.SDK
    if any(_localname(child) == "sdk" for child in root):
        return ProjectStyle.SDK
    if any(
        _localname(child) == "import" and child.get("Sdk")
        for child in root.iter()
    ):
        return ProjectStyle.SDK

    # A legacy project imports the C# targets by path instead.
    for element in root.iter():
        if _localname(element) != "import":
            continue
        project_attribute = element.get("Project") or ""
        if "Microsoft.CSharp.targets" in project_attribute:
            return ProjectStyle.LEGACY

    if root.get("ToolsVersion"):
        return ProjectStyle.LEGACY

    return ProjectStyle.UNKNOWN


def _read_properties(root: ET.Element) -> dict[str, str]:
    """Flatten PropertyGroup contents, lower-cased by name.

    Conditions are ignored on purpose: this is a cheap survey, not an MSBuild
    evaluation. Only the extractor's real load knows which condition wins, and
    for the questions asked here an over-inclusive read is the safe direction --
    it is better to flag Windows as possibly required than to miss it.
    """
    properties: dict[str, str] = {}
    for group in root.iter():
        if _localname(group) != "propertygroup":
            continue
        for element in group:
            name = _localname(element)
            text = (element.text or "").strip()
            if text and name not in properties:
                properties[name] = text
    return properties


def _read_target_frameworks(properties: dict[str, str]) -> tuple[str, ...]:
    if plural := properties.get("targetframeworks"):
        return tuple(
            part.strip() for part in plural.split(";") if part.strip()
        )
    if single := properties.get("targetframework"):
        return (single,)
    # Legacy projects say v4.8 rather than net48.
    if legacy := properties.get("targetframeworkversion"):
        return (_normalize_legacy_framework(legacy),)
    return ()


def _normalize_legacy_framework(version: str) -> str:
    """v4.8 -> net48, v4.7.2 -> net472, v3.5 -> net35."""
    digits = re.sub(r"[^0-9]", "", version)
    return f"net{digits}" if digits else version


def _windows_reasons(
    properties: dict[str, str], frameworks: Iterable[str], style: ProjectStyle
) -> tuple[str, ...]:
    """Why this project cannot be analyzed on Linux, if it cannot."""
    reasons: list[str] = []

    for framework in frameworks:
        lowered = framework.lower()
        if "-windows" in lowered:
            reasons.append(f"{framework} is a Windows-only target framework")
        elif re.fullmatch(r"net[1-4]\d*", lowered):
            # net48, net472, net35: .NET Framework, no Linux targeting pack.
            reasons.append(
                f"{framework} is .NET Framework; MSBuild needs the Windows "
                "targeting pack to resolve its references"
            )

    for flag, description in (
        ("usewpf", "WPF"),
        ("usewindowsforms", "WinForms"),
    ):
        if properties.get(flag, "").lower() == "true":
            reasons.append(f"uses {description}, which is Windows-only")

    if style is ProjectStyle.LEGACY:
        reasons.append(
            "non-SDK project format; MSBuildWorkspace support outside Windows is unreliable"
        )

    return tuple(dict.fromkeys(reasons))


def _is_restored(project_path: Path, style: ProjectStyle) -> bool:
    """Has restore produced what the compiler needs to resolve references?"""
    if style is ProjectStyle.LEGACY:
        # packages.config restore drops assemblies in a solution-level packages/
        # folder; without one, references resolve only if they are all framework
        # assemblies. Treat the folder's presence as the best cheap signal.
        for ancestor in project_path.parents:
            if (ancestor / "packages").is_dir():
                return True
        return not (project_path.parent / "packages.config").exists()

    return (project_path.parent / "obj" / "project.assets.json").exists()


def _decide(probe: RepoProbe) -> None:
    if not probe.projects:
        probe.verdict = Verdict.UNKNOWN
        probe.reasons.append("no .csproj files found")
        return

    needs_windows = probe.windows_only_projects
    probe.restore_needed = [p.project_key for p in probe.projects if not p.restored]

    unparsed = [p for p in probe.projects if p.parse_error]
    for project in unparsed:
        probe.reasons.append(f"{project.project_key}: could not be parsed ({project.parse_error})")

    if not needs_windows:
        probe.verdict = Verdict.LINUX_OK
        probe.reasons.append(
            "all projects are SDK-style and cross-platform; a Linux container can analyze them"
        )
        return

    if len(needs_windows) == len(probe.projects):
        probe.verdict = Verdict.NEEDS_WINDOWS
    else:
        probe.verdict = Verdict.MIXED
        probe.reasons.append(
            f"{len(probe.projects) - len(needs_windows)} of {len(probe.projects)} projects "
            "can be analyzed on Linux; the rest need a Windows host"
        )

    # Group the reasons so the output stays readable on a large solution.
    counted: dict[str, int] = {}
    for project in needs_windows:
        for reason in project.windows_reasons:
            counted[reason] = counted.get(reason, 0) + 1
    for reason, count in sorted(counted.items(), key=lambda item: -item[1]):
        probe.reasons.append(f"{count} project(s): {reason}")


def _walk(root: Path, suffixes: set[str]) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.suffix.lower() not in suffixes or not path.is_file():
            continue
        if any(part in _SKIP_DIRECTORIES for part in path.relative_to(root).parts[:-1]):
            continue
        yield path


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def _localname(element: ET.Element) -> str:
    """Tag without its namespace, lower-cased.

    Legacy projects live in the 2003 MSBuild namespace and SDK-style ones have no
    namespace at all, so every tag comparison has to go through here.
    """
    tag = element.tag
    if isinstance(tag, str) and "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    return tag.lower() if isinstance(tag, str) else ""


def _main(argv: list[str] | None = None) -> int:
    """Command line entry point:

        .venv/bin/python -m app.preflight /path/to/codebase [--json]
    """
    import argparse
    import dataclasses
    import json
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m app.preflight",
        description="Report what kind of .NET codebase this is, without MSBuild.",
    )
    parser.add_argument("repo_root", help="Directory to probe.")
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    parser.add_argument(
        "--per-project", action="store_true", help="List every project, not just a summary."
    )
    arguments = parser.parse_args(argv)

    probe = probe_repository(arguments.repo_root)

    if arguments.json:
        payload = dataclasses.asdict(probe)
        payload["verdict"] = str(probe.verdict)
        for project in payload["projects"]:
            project["style"] = str(project["style"])
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(probe.summary())
        if arguments.per_project:
            print()
            for project in probe.projects:
                frameworks = ",".join(project.target_frameworks) or "?"
                marker = "windows" if project.needs_windows else "linux"
                restored = "" if project.restored else "  [not restored]"
                print(f"  {marker:8} {project.style:7} {frameworks:24} {project.project_key}{restored}")

    # Exit code carries the verdict so a shell or CI step can branch on it.
    return {
        Verdict.LINUX_OK: 0,
        Verdict.MIXED: 1,
        Verdict.NEEDS_WINDOWS: 2,
        Verdict.UNKNOWN: 3,
    }[probe.verdict]


if __name__ == "__main__":
    raise SystemExit(_main())
