using System.Diagnostics;

namespace CodeGraphify.Extractor.Inventory;

/// <summary>One file the repository tracks.</summary>
public sealed record RepositoryFile(string RelativePath, string AbsolutePath, string Category);

/// <summary>
/// Stage 1, repository half: what files exist according to the repo itself.
///
/// git is preferred over a filesystem walk because it already knows what is
/// source and what is build output, so bin/ and obj/ never enter the graph. A
/// non-git directory falls back to walking the filesystem with those exclusions
/// applied by hand.
/// </summary>
public static class RepositoryInventory
{
    private static readonly HashSet<string> ExcludedDirectories = new(StringComparer.OrdinalIgnoreCase)
    {
        "bin", "obj", ".git", ".vs", "node_modules", "packages", ".idea", "TestResults",
    };

    private static readonly Dictionary<string, string> CategoryByExtension = new(StringComparer.OrdinalIgnoreCase)
    {
        [".cs"] = "code",
        [".csx"] = "code",
        [".vb"] = "code",
        [".fs"] = "code",
        [".razor"] = "code",
        [".cshtml"] = "code",
        [".csproj"] = "project",
        [".vbproj"] = "project",
        [".fsproj"] = "project",
        [".sln"] = "solution",
        [".props"] = "build",
        [".targets"] = "build",
        [".config"] = "configuration",
        [".json"] = "configuration",
        [".yml"] = "configuration",
        [".yaml"] = "configuration",
        [".xml"] = "configuration",
        [".editorconfig"] = "configuration",
        [".md"] = "documentation",
        [".txt"] = "documentation",
    };

    public static string? FindGitRoot(string startDirectory)
    {
        var result = RunGit(startDirectory, "rev-parse", "--show-toplevel");
        if (result is null)
        {
            return null;
        }

        var root = result.Trim();
        return root.Length == 0 ? null : Path.GetFullPath(root);
    }

    public static IReadOnlyList<RepositoryFile> Collect(string repoRoot, out bool usedGit)
    {
        var tracked = ListGitFiles(repoRoot);
        usedGit = tracked is not null;
        var relativePaths = tracked ?? WalkFilesystem(repoRoot);

        var files = new List<RepositoryFile>();
        foreach (var relative in relativePaths)
        {
            var category = Categorize(relative);
            if (category is null)
            {
                continue;
            }

            files.Add(new RepositoryFile(
                relative,
                Path.Combine(repoRoot, relative.Replace('/', Path.DirectorySeparatorChar)),
                category));
        }

        return files;
    }

    private static string? Categorize(string relativePath)
    {
        var name = Path.GetFileName(relativePath);
        if (CategoryByExtension.TryGetValue(name, out var byName))
        {
            // .editorconfig and friends are all extension, no stem.
            return byName;
        }

        var extension = Path.GetExtension(relativePath);
        return CategoryByExtension.TryGetValue(extension, out var category) ? category : null;
    }

    /// <summary>Tracked paths from git, or null when this is not a git repo.</summary>
    private static IReadOnlyList<string>? ListGitFiles(string repoRoot)
    {
        // -z because paths may contain anything but NUL; without it git quotes
        // and escapes unusual names and the paths come back wrong.
        var output = RunGit(repoRoot, "-C", repoRoot, "ls-files", "-z");
        if (output is null)
        {
            return null;
        }

        return output
            .Split('\0', StringSplitOptions.RemoveEmptyEntries)
            .Where(path => !IsExcluded(path))
            .ToList();
    }

    private static IReadOnlyList<string> WalkFilesystem(string repoRoot)
    {
        var results = new List<string>();
        var pending = new Stack<string>();
        pending.Push(repoRoot);

        while (pending.Count > 0)
        {
            var directory = pending.Pop();

            IEnumerable<string> subdirectories;
            IEnumerable<string> entries;
            try
            {
                subdirectories = Directory.EnumerateDirectories(directory);
                entries = Directory.EnumerateFiles(directory);
            }
            catch (Exception e) when (e is UnauthorizedAccessException or IOException)
            {
                continue;
            }

            foreach (var subdirectory in subdirectories)
            {
                if (!ExcludedDirectories.Contains(Path.GetFileName(subdirectory)))
                {
                    pending.Push(subdirectory);
                }
            }

            foreach (var entry in entries)
            {
                results.Add(PathUtil.ToRepoRelative(repoRoot, entry));
            }
        }

        return results;
    }

    private static bool IsExcluded(string relativePath) =>
        relativePath
            .Split('/')
            .SkipLast(1)
            .Any(segment => ExcludedDirectories.Contains(segment));

    private static string? RunGit(string workingDirectory, params string[] args)
    {
        var startInfo = new ProcessStartInfo("git")
        {
            WorkingDirectory = workingDirectory,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
        };

        foreach (var arg in args)
        {
            startInfo.ArgumentList.Add(arg);
        }

        try
        {
            using var process = Process.Start(startInfo);
            if (process is null)
            {
                return null;
            }

            var stdout = process.StandardOutput.ReadToEnd();
            process.StandardError.ReadToEnd();
            process.WaitForExit();
            return process.ExitCode == 0 ? stdout : null;
        }
        catch (Exception e) when (e is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            // git is not installed: fall back to the filesystem walk.
            return null;
        }
    }
}
