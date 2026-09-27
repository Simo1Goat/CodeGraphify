namespace CodeGraphify.Extractor.Inventory;

public static class PathUtil
{
    /// <summary>
    /// Repo-relative, forward-slashed path. Every path in the graph goes through
    /// here so ids stay identical whether the repo was scanned on Linux or
    /// Windows, and wherever it happens to be checked out.
    /// </summary>
    public static string ToRepoRelative(string repoRoot, string absolutePath)
    {
        var full = Path.GetFullPath(absolutePath);
        var relative = Path.GetRelativePath(repoRoot, full);
        return relative.Replace('\\', '/');
    }

    /// <summary>True when the path lies outside the repo root (a referenced
    /// project elsewhere on disk, or a generated file under a temp directory).</summary>
    public static bool IsOutsideRepo(string repoRelativePath) =>
        repoRelativePath.StartsWith("../", StringComparison.Ordinal) || Path.IsPathRooted(repoRelativePath);
}
