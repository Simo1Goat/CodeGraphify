using CodeGraphify.Extractor.Inventory;

namespace CodeGraphify.Extractor.Symbols;

/// <summary>
/// Which assemblies belong to projects in this scan.
///
/// The distinction that matters for the graph is not "declared in this
/// compilation" but "declared in a project we analyzed". A call from Api into
/// Orders crosses a compilation boundary while staying entirely inside the
/// codebase, and treating it as external would both mislabel it and point it at a
/// duplicate node instead of the real declaration stage 2 emitted.
/// </summary>
public sealed class AnalyzedScope
{
    private readonly Dictionary<string, List<Variant>> _byAssemblyName =
        new(StringComparer.OrdinalIgnoreCase);

    public readonly record struct Variant(string ProjectKey, string TargetFramework, bool IsPrimary);

    public static AnalyzedScope From(WorkspaceLoadResult loaded)
    {
        var scope = new AnalyzedScope();

        foreach (var project in loaded.Projects)
        {
            foreach (var variant in project.Variants)
            {
                // A degraded variant has no trustworthy symbols, so a call into it
                // is better recorded as external than pointed at a node that was
                // never emitted.
                if (variant.IsDegraded)
                {
                    continue;
                }

                var assemblyName = variant.Compilation?.AssemblyName ?? variant.Project.AssemblyName;
                if (string.IsNullOrEmpty(assemblyName))
                {
                    continue;
                }

                if (!scope._byAssemblyName.TryGetValue(assemblyName, out var variants))
                {
                    variants = [];
                    scope._byAssemblyName[assemblyName] = variants;
                }

                variants.Add(new Variant(project.ProjectKey, variant.TargetFramework, variant.IsPrimary));
            }
        }

        return scope;
    }

    /// <summary>
    /// The variant a call target belongs to, if it is one of ours.
    ///
    /// Multi-targeting makes this a choice rather than a lookup: one assembly name
    /// can map to several variants. A caller compiled for net8.0 references the
    /// net8.0 build of its dependency, so an exact target-framework match wins;
    /// failing that the primary variant, which is the one a default scan emits.
    /// </summary>
    public bool TryResolve(string? assemblyName, string callerTargetFramework, out Variant match)
    {
        match = default;

        if (assemblyName is null || !_byAssemblyName.TryGetValue(assemblyName, out var variants))
        {
            return false;
        }

        foreach (var variant in variants)
        {
            if (string.Equals(variant.TargetFramework, callerTargetFramework, StringComparison.OrdinalIgnoreCase))
            {
                match = variant;
                return true;
            }
        }

        var primary = variants.FirstOrDefault(v => v.IsPrimary);
        match = primary.ProjectKey is null ? variants[0] : primary;
        return true;
    }
}
