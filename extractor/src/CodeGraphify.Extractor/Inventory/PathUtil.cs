namespace CodeGraphify.Extractor.Inventory;

public static class PathUtil
{
    /// <summary>
    /// Repo-relative, forward-slashed path. Every path in the graph goes through
    /// here so ids stay identical whether the repo was scanned on Linux or
    /// Windows, and wherever it happens to be checked out.
    /// </summary>
    /// <summary>Prefix marking a path that exists only inside a compilation.</summary>
    public const string GeneratedPrefix = "<generated>/";

    public static string ToRepoRelative(string repoRoot, string absolutePath)
    {
        if (string.IsNullOrEmpty(absolutePath))
        {
            return absolutePath;
        }

        // A source generator names its output with a synthetic path rather than a real
        // one ("Razor.SourceGenerators/.../App_razor.g.cs"). Resolving that against the
        // working directory would invent a location on disk, so it is kept verbatim and
        // marked instead.
        if (!Path.IsPathRooted(absolutePath))
        {
            return GeneratedPrefix + absolutePath.Replace('\\', '/');
        }

        var full = Path.GetFullPath(absolutePath);
        var relative = Path.GetRelativePath(repoRoot, full);
        return relative.Replace('\\', '/');
    }

    /// <summary>True when the path lies outside the repo root (a referenced
    /// project elsewhere on disk, or a generated file under a temp directory).</summary>
    public static bool IsOutsideRepo(string repoRelativePath) =>
        repoRelativePath.StartsWith("../", StringComparison.Ordinal) || Path.IsPathRooted(repoRelativePath);
}
