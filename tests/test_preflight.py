"""The preflight probe: what kind of .NET codebase is this?

It must work with no SDK, no restore and no MSBuild, because its whole purpose is
to answer the question before, and independently of, any attempt to analyze.
"""

from __future__ import annotations

from pathlib import Path

from app.preflight import (
    ProjectStyle,
    Verdict,
    probe_project,
    probe_repository,
)
from tests.conftest import REPO_ROOT

SDK_FIXTURE = REPO_ROOT / "extractor" / "fixtures" / "SampleSolution"
LEGACY_FIXTURE = REPO_ROOT / "extractor" / "fixtures" / "LegacySolution"


def test_a_modern_cross_platform_solution_can_run_on_linux() -> None:
    probe = probe_repository(SDK_FIXTURE)
    assert probe.verdict is Verdict.LINUX_OK
    assert probe.windows_only_projects == []
    assert {p.style for p in probe.projects} == {ProjectStyle.SDK}


def test_multi_targeting_is_reported_as_several_frameworks() -> None:
    probe = probe_repository(SDK_FIXTURE)
    core = next(p for p in probe.projects if p.project_key.endswith("Core.csproj"))
    assert core.target_frameworks == ("net8.0", "netstandard2.0")


def test_a_legacy_solution_is_reported_as_needing_windows() -> None:
    probe = probe_repository(LEGACY_FIXTURE)
    assert probe.verdict is Verdict.NEEDS_WINDOWS
    assert len(probe.windows_only_projects) == 2


def test_non_sdk_project_format_is_detected() -> None:
    probe = probe_project(
        LEGACY_FIXTURE / "src" / "Legacy.Orders" / "Legacy.Orders.csproj", LEGACY_FIXTURE
    )
    assert probe.style is ProjectStyle.LEGACY
    assert probe.uses_packages_config is True
    assert probe.assembly_name == "Legacy.Orders"


def test_legacy_framework_version_is_normalized_to_a_moniker() -> None:
    """A legacy project says v4.8; the graph and every report say net48."""
    probe = probe_project(
        LEGACY_FIXTURE / "src" / "Legacy.Orders" / "Legacy.Orders.csproj", LEGACY_FIXTURE
    )
    assert probe.target_frameworks == ("net48",)


def test_a_modern_project_can_still_require_windows() -> None:
    """Project format and platform requirement are independent questions.

    Desktop.csproj is SDK-style, so a format check alone would pass it, but
    net8.0-windows plus WPF cannot be analyzed on Linux.
    """
    probe = probe_project(LEGACY_FIXTURE / "src" / "Desktop" / "Desktop.csproj", LEGACY_FIXTURE)
    assert probe.style is ProjectStyle.SDK
    assert probe.needs_windows is True
    reasons = " ".join(probe.windows_reasons)
    assert "net8.0-windows" in reasons
    assert "WPF" in reasons


def test_restore_state_is_reported(tmp_path: Path) -> None:
    """Unrestored projects are the usual cause of a degraded scan, so the probe
    reports them before a scan is attempted."""
    project = tmp_path / "A"
    project.mkdir()
    (project / "A.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
        "<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>"
    )

    assert probe_project(project / "A.csproj", tmp_path).restored is False

    (project / "obj").mkdir()
    (project / "obj" / "project.assets.json").write_text("{}")
    assert probe_project(project / "A.csproj", tmp_path).restored is True


def test_the_sample_fixture_is_restored_because_the_tests_build_it(
    fixture_built: None,
) -> None:
    probe = probe_repository(SDK_FIXTURE)
    assert probe.restore_needed == []


def test_a_mixed_repository_is_reported_as_mixed(tmp_path: Path) -> None:
    """Mixed matters: part of the graph can be built on Linux and part cannot."""
    for name, framework in (("Portable", "net8.0"), ("Windowsy", "net8.0-windows")):
        directory = tmp_path / name
        directory.mkdir()
        (directory / f"{name}.csproj").write_text(
            f'<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
            f"<TargetFramework>{framework}</TargetFramework></PropertyGroup></Project>"
        )

    probe = probe_repository(tmp_path)
    assert probe.verdict is Verdict.MIXED
    assert len(probe.windows_only_projects) == 1
    assert any("can be analyzed on Linux" in reason for reason in probe.reasons)


def test_legacy_msbuild_namespace_is_handled(tmp_path: Path) -> None:
    """Legacy projects live in the 2003 MSBuild XML namespace.

    Every tag comparison has to strip it, or a legacy project reads as having no
    properties at all and silently looks cross-platform.
    """
    project = tmp_path / "Old"
    project.mkdir()
    (project / "Old.csproj").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<Project ToolsVersion="15.0" '
        'xmlns="http://schemas.microsoft.com/developer/msbuild/2003">'
        "<PropertyGroup><TargetFrameworkVersion>v4.7.2</TargetFrameworkVersion>"
        "<AssemblyName>Old</AssemblyName></PropertyGroup>"
        '<Import Project="$(MSBuildToolsPath)\\Microsoft.CSharp.targets" />'
        "</Project>"
    )

    probe = probe_project(project / "Old.csproj", tmp_path)
    assert probe.style is ProjectStyle.LEGACY
    assert probe.target_frameworks == ("net472",)
    assert probe.assembly_name == "Old"
    assert probe.needs_windows is True


def test_unparseable_project_is_reported_not_raised(tmp_path: Path) -> None:
    """One malformed file must not abort a survey of a large repository."""
    (tmp_path / "Broken.csproj").write_text("<Project> unclosed")
    probe = probe_repository(tmp_path)
    assert len(probe.projects) == 1
    assert probe.projects[0].parse_error is not None
    assert probe.projects[0].style is ProjectStyle.UNKNOWN
    assert any("could not be parsed" in reason for reason in probe.reasons)


def test_build_output_is_not_probed(tmp_path: Path) -> None:
    """A copied project under obj/ would double-count and skew the verdict."""
    (tmp_path / "Real.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
        "<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>"
    )
    buried = tmp_path / "obj" / "Debug"
    buried.mkdir(parents=True)
    (buried / "Copy.csproj").write_text("<Project />")

    probe = probe_repository(tmp_path)
    assert [p.project_key for p in probe.projects] == ["Real.csproj"]


def test_an_empty_directory_is_unknown_not_linux_ok(tmp_path: Path) -> None:
    """Finding nothing must never read as a clean bill of health."""
    probe = probe_repository(tmp_path)
    assert probe.verdict is Verdict.UNKNOWN


def test_summary_is_human_readable() -> None:
    summary = probe_repository(LEGACY_FIXTURE).summary()
    assert "needs-windows" in summary
    assert "net48" in summary


# --- centrally managed properties ---------------------------------------------
#
# Found on a real repository: eShopOnWeb declares TargetFramework in
# Directory.Packages.props and not in any .csproj, so reading only project files
# found no framework at all. That is not cosmetic -- with no frameworks to inspect,
# the Windows checks have nothing to match, and a Windows-only repo that centralises
# its TFM would be reported linux-ok. That is the one wrong answer with a real
# consequence: routing a codebase to a host that cannot analyze it.


def _sdk_project(directory: Path, name: str, body: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.csproj"
    path.write_text(f'<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>{body}</PropertyGroup></Project>')
    return path


def test_target_framework_is_read_from_directory_build_props(tmp_path: Path) -> None:
    (tmp_path / "Directory.Build.props").write_text(
        "<Project><PropertyGroup><TargetFramework>net8.0</TargetFramework>"
        "</PropertyGroup></Project>"
    )
    project = _sdk_project(tmp_path / "src" / "A", "A")

    probe = probe_project(project, tmp_path)
    assert probe.target_frameworks == ("net8.0",)
    assert probe.target_framework_source == "imported:Directory.Build.props"


def test_target_framework_is_read_from_directory_packages_props(tmp_path: Path) -> None:
    """Where eShopOnWeb actually puts it: NuGet's central package management file,
    which MSBuild imports just as automatically."""
    (tmp_path / "Directory.Packages.props").write_text(
        "<Project><PropertyGroup><ManagePackageVersionsCentrally>true"
        "</ManagePackageVersionsCentrally><TargetFramework>net8.0</TargetFramework>"
        "</PropertyGroup></Project>"
    )
    project = _sdk_project(tmp_path / "src" / "A", "A")

    probe = probe_project(project, tmp_path)
    assert probe.target_frameworks == ("net8.0",)
    assert probe.target_framework_source == "imported:Directory.Packages.props"


def test_a_project_overrides_an_imported_framework(tmp_path: Path) -> None:
    (tmp_path / "Directory.Build.props").write_text(
        "<Project><PropertyGroup><TargetFramework>net8.0</TargetFramework>"
        "</PropertyGroup></Project>"
    )
    project = _sdk_project(
        tmp_path / "src" / "A", "A", "<TargetFramework>net6.0</TargetFramework>"
    )

    probe = probe_project(project, tmp_path)
    assert probe.target_frameworks == ("net6.0",)
    assert probe.target_framework_source == "project"


def test_the_nearest_directory_props_wins(tmp_path: Path) -> None:
    (tmp_path / "Directory.Build.props").write_text(
        "<Project><PropertyGroup><TargetFramework>net6.0</TargetFramework>"
        "</PropertyGroup></Project>"
    )
    nested = tmp_path / "src"
    nested.mkdir()
    (nested / "Directory.Build.props").write_text(
        "<Project><PropertyGroup><TargetFramework>net8.0</TargetFramework>"
        "</PropertyGroup></Project>"
    )
    project = _sdk_project(nested / "A", "A")

    assert probe_project(project, tmp_path).target_frameworks == ("net8.0",)


def test_a_centrally_managed_windows_only_repo_is_not_reported_as_linux_ok(
    tmp_path: Path,
) -> None:
    """The bug that mattered. Without following imports this returned linux-ok,
    which would route a Windows-only codebase to a Linux host."""
    (tmp_path / "Directory.Build.props").write_text(
        "<Project><PropertyGroup><TargetFramework>net48</TargetFramework>"
        "<UseWindowsForms>true</UseWindowsForms></PropertyGroup></Project>"
    )
    _sdk_project(tmp_path / "src" / "App", "App")

    probe = probe_repository(tmp_path)
    assert probe.verdict is Verdict.NEEDS_WINDOWS
    reasons = " ".join(probe.reasons)
    assert "net48" in reasons
    assert "WinForms" in reasons


def test_an_explicitly_imported_props_file_is_followed(tmp_path: Path) -> None:
    (tmp_path / "common.props").write_text(
        "<Project><PropertyGroup><TargetFramework>net8.0</TargetFramework>"
        "</PropertyGroup></Project>"
    )
    directory = tmp_path / "src" / "A"
    directory.mkdir(parents=True)
    (directory / "A.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk">'
        '<Import Project="../../common.props" />'
        "</Project>"
    )

    assert probe_project(directory / "A.csproj", tmp_path).target_frameworks == ("net8.0",)


def test_an_unevaluatable_import_path_is_skipped(tmp_path: Path) -> None:
    """A legacy project imports $(MSBuildToolsPath)\\Microsoft.CSharp.targets.
    Following that would need MSBuild, which is exactly what this module avoids."""
    directory = tmp_path / "src" / "A"
    directory.mkdir(parents=True)
    (directory / "A.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk">'
        '<Import Project="$(MSBuildToolsPath)\\Microsoft.CSharp.targets" />'
        "<PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>"
        "</Project>"
    )
    probe = probe_project(directory / "A.csproj", tmp_path)
    assert probe.target_frameworks == ("net8.0",)
    assert probe.parse_error is None
