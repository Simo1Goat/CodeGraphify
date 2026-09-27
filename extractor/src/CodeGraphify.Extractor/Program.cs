using System.Runtime.CompilerServices;
using System.Text.Json;
using CodeGraphify.Extractor.Cli;
using CodeGraphify.Extractor.Contract;
using CodeGraphify.Extractor.Output;

namespace CodeGraphify.Extractor;

public static class Program
{
    /// <summary>Exit codes are half of the subprocess contract: Python decides
    /// whether to ingest, and whether to fall back, from these plus the manifest.</summary>
    private const int ExitSuccess = 0;
    private const int ExitUsage = 2;
    private const int ExitNoMSBuild = 3;
    private const int ExitFailed = 4;

    public static async Task<int> Main(string[] args)
    {
        if (args.Length == 0 || args.Contains("--help") || args.Contains("-h"))
        {
            Console.Error.WriteLine(ExtractorOptions.Usage);
            return ExitUsage;
        }

        ExtractorOptions options;
        try
        {
            options = ExtractorOptions.Parse(args);
        }
        catch (Exception e) when (e is ArgumentException or FormatException)
        {
            Console.Error.WriteLine($"error: {e.Message}");
            Console.Error.WriteLine();
            Console.Error.WriteLine(ExtractorOptions.Usage);
            return ExitUsage;
        }

        try
        {
            // MSBuildLocator must register before any MSBuild or MSBuildWorkspace
            // type is loaded. The JIT resolves a method's types when that method
            // is first entered, so registration happens here and everything that
            // touches those types lives behind a NoInlining boundary in Run.
            var instance = Inventory.WorkspaceLoader.RegisterMSBuild();
            if (options.Verbose)
            {
                Console.Error.WriteLine($"[msbuild] {instance.Name} {instance.Version} at {instance.MSBuildPath}");
            }
        }
        catch (InvalidOperationException e)
        {
            Console.Error.WriteLine($"error: {e.Message}");
            await WriteFailureManifestAsync(options, e.Message);
            return ExitNoMSBuild;
        }

        return await RunAsync(options);
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static async Task<int> RunAsync(ExtractorOptions options)
    {
        var startedAt = DateTimeOffset.UtcNow;
        using var cancellation = new CancellationTokenSource();
        Console.CancelKeyPress += (_, e) =>
        {
            e.Cancel = true;
            cancellation.Cancel();
        };

        var repoRoot = ResolveRepoRoot(options);
        if (options.Verbose)
        {
            Console.Error.WriteLine($"[repo] root={repoRoot} id={options.RepoId} scan={options.ScanId}");
        }

        await using var output = new GraphOutput(options.OutputDirectory);
        var errors = new List<string>();
        var analyzed = new List<string>();
        var failed = new List<string>();
        AnalysisMode mode;

        try
        {
            var files = Inventory.RepositoryInventory.Collect(repoRoot, out var usedGit);
            if (options.Verbose)
            {
                Console.Error.WriteLine($"[inventory] {files.Count} files via {(usedGit ? "git" : "filesystem")}");
            }

            var loader = new Inventory.WorkspaceLoader(options, repoRoot, Console.Error);
            var loaded = await loader.LoadAsync(cancellation.Token);

            var emitter = new Inventory.InventoryEmitter(options, repoRoot, output);
            emitter.EmitRepository(files, usedGit, loaded.SolutionPath);
            emitter.EmitProjects(loaded);

            foreach (var diagnostic in loaded.Diagnostics)
            {
                output.WriteDiagnostic(diagnostic);
            }

            foreach (var project in loaded.Projects)
            {
                // A project counts as analyzed only when its primary variant
                // produced a usable compilation. Anything less and the stale
                // sweep must not touch it.
                var primary = project.Variants.FirstOrDefault(v => v.IsPrimary) ?? project.Variants[0];
                if (primary.Compilation is not null && !primary.IsDegraded)
                {
                    analyzed.Add(project.ProjectKey);
                }
                else
                {
                    failed.Add(project.ProjectKey);
                }
            }

            mode = DecideMode(loaded, analyzed, failed);

            if (loaded.Projects.Count == 0)
            {
                errors.Add("No C# projects were loaded.");
            }
        }
        catch (OperationCanceledException)
        {
            errors.Add("Extraction was cancelled.");
            mode = AnalysisMode.Failed;
        }
        catch (Exception e)
        {
            // Never let an exception escape as a stack trace on stdout: stdout is
            // the manifest channel and Python parses it.
            Console.Error.WriteLine($"error: {e}");
            errors.Add($"{e.GetType().Name}: {e.Message}");
            mode = AnalysisMode.Failed;
        }

        var manifest = new Manifest
        {
            RepoId = options.RepoId,
            ScanId = options.ScanId,
            AnalysisMode = mode.ToWire(),
            StartedAt = startedAt.ToString("O"),
            FinishedAt = DateTimeOffset.UtcNow.ToString("O"),
            RepoRoot = repoRoot,
            AnalyzedProjectKeys = [.. analyzed.Order(StringComparer.Ordinal)],
            FailedProjectKeys = [.. failed.Order(StringComparer.Ordinal)],
            Counts = new Dictionary<string, int>
            {
                ["entities"] = output.EntityCount,
                ["relations"] = output.RelationCount,
                ["compilations"] = output.CompilationCount,
                ["diagnostics"] = output.DiagnosticCount,
                ["projects_analyzed"] = analyzed.Count,
                ["projects_failed"] = failed.Count,
            },
            OutputFiles = output.DescribeFiles(),
            Errors = [.. errors],
        };

        await WriteManifestAsync(options.OutputDirectory, manifest);
        return mode == AnalysisMode.Failed ? ExitFailed : ExitSuccess;
    }

    /// <summary>
    /// Semantic only when every project compiled. One degraded project makes the
    /// whole scan partial-semantic, because the writer keys its stale-sweep
    /// decision off this value and a wrong "semantic" deletes real nodes.
    /// </summary>
    private static AnalysisMode DecideMode(
        Inventory.WorkspaceLoadResult loaded,
        List<string> analyzed,
        List<string> failed)
    {
        if (loaded.Projects.Count == 0)
        {
            return AnalysisMode.Failed;
        }

        if (analyzed.Count == 0)
        {
            // Projects loaded but nothing compiled: the syntax trees are still
            // there, so the output is worth ingesting with reduced trust.
            return loaded.Projects.Any(p => p.Variants.Any(v => v.Project.Documents.Any()))
                ? AnalysisMode.SyntaxOnly
                : AnalysisMode.Failed;
        }

        return failed.Count == 0 ? AnalysisMode.Semantic : AnalysisMode.PartialSemantic;
    }

    private static async Task WriteManifestAsync(string outputDirectory, Manifest manifest)
    {
        var json = JsonSerializer.Serialize(manifest, new JsonSerializerOptions(GraphOutput.JsonOptions)
        {
            WriteIndented = true,
        });

        Directory.CreateDirectory(outputDirectory);
        await File.WriteAllTextAsync(Path.Combine(outputDirectory, GraphOutput.ManifestFileName), json + "\n");

        // stdout carries only the manifest, so Python can parse it without
        // filtering log noise.
        Console.Out.WriteLine(json);
    }

    private static async Task WriteFailureManifestAsync(ExtractorOptions options, string error)
    {
        var now = DateTimeOffset.UtcNow.ToString("O");
        await WriteManifestAsync(options.OutputDirectory, new Manifest
        {
            RepoId = options.RepoId,
            ScanId = options.ScanId,
            AnalysisMode = AnalysisMode.Failed.ToWire(),
            StartedAt = now,
            FinishedAt = now,
            RepoRoot = options.RepoRoot ?? options.Input,
            Errors = [error],
        });
    }

    private static string ResolveRepoRoot(ExtractorOptions options)
    {
        if (options.RepoRoot is not null)
        {
            return options.RepoRoot;
        }

        var start = Directory.Exists(options.Input)
            ? options.Input
            : Path.GetDirectoryName(options.Input)!;

        return Inventory.RepositoryInventory.FindGitRoot(start) ?? start;
    }
}
