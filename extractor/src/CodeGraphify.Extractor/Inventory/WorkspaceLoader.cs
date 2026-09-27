using CodeGraphify.Extractor.Cli;
using Microsoft.Build.Locator;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.MSBuild;

namespace CodeGraphify.Extractor.Inventory;

/// <summary>One compilation variant: a project as compiled for one target framework.</summary>
public sealed class LoadedVariant
{
    public required Project Project { get; init; }

    public required string TargetFramework { get; init; }

    public required bool IsPrimary { get; init; }

    /// <summary>Null when the compilation could not be produced at all.</summary>
    public Compilation? Compilation { get; set; }

    /// <summary>Errors that mean references were missing (usually: never restored).</summary>
    public int MissingReferenceErrorCount { get; set; }

    public bool IsDegraded { get; set; }
}

/// <summary>A .csproj and every variant of it the workspace produced.</summary>
public sealed class LoadedProject
{
    public required string ProjectKey { get; init; }

    public required string ProjectPath { get; init; }

    public List<LoadedVariant> Variants { get; } = [];
}

public sealed class WorkspaceLoadResult
{
    public List<LoadedProject> Projects { get; } = [];

    public List<Contract.Diagnostic> Diagnostics { get; } = [];

    public string? SolutionPath { get; set; }
}

/// <summary>
/// Stage 1, compilation half: which documents belong to which project and which
/// compilation variant, according to MSBuild rather than to a guess.
/// </summary>
public sealed class WorkspaceLoader
{
    private readonly ExtractorOptions _options;
    private readonly string _repoRoot;
    private readonly TextWriter _log;

    public WorkspaceLoader(ExtractorOptions options, string repoRoot, TextWriter log)
    {
        _options = options;
        _repoRoot = repoRoot;
        _log = log;
    }

    /// <summary>
    /// Registers the MSBuild the machine actually has. Must run before any
    /// MSBuild or MSBuildWorkspace type is loaded, which is why the caller keeps
    /// this out of the method that touches those types.
    /// </summary>
    public static VisualStudioInstance RegisterMSBuild()
    {
        if (MSBuildLocator.IsRegistered)
        {
            return MSBuildLocator.QueryVisualStudioInstances().First();
        }

        var instances = MSBuildLocator.QueryVisualStudioInstances().ToList();
        if (instances.Count == 0)
        {
            throw new InvalidOperationException(
                "No .NET SDK (MSBuild) found. MSBuildWorkspace cannot open projects without it. " +
                "Install the .NET SDK on the machine running the extractor.");
        }

        // Highest version wins: it can still open projects targeting older
        // frameworks, while an old MSBuild cannot open newer project formats.
        var chosen = instances.OrderByDescending(i => i.Version).First();
        MSBuildLocator.RegisterInstance(chosen);
        return chosen;
    }

    public async Task<WorkspaceLoadResult> LoadAsync(CancellationToken cancellationToken)
    {
        var result = new WorkspaceLoadResult();

        // Debug/Release matters: it changes DefineConstants, so it changes which
        // syntax is active. It is part of the variant's identity.
        var properties = new Dictionary<string, string>
        {
            ["Configuration"] = _options.Configuration,
            // Design-time-ish load: we want references and compile items, not
            // actual assemblies on disk.
            ["DesignTimeBuild"] = "true",
            ["BuildingInsideVisualStudio"] = "true",
            ["ProvideCommandLineArgs"] = "true",
            ["SkipCompilerExecution"] = "true",
        };

        using var workspace = MSBuildWorkspace.Create(properties);

        // A project that fails to load must be visible in the output, not
        // silently missing: it is the difference between semantic and
        // partial-semantic.
        workspace.WorkspaceFailed += (_, e) =>
        {
            var severity = e.Diagnostic.Kind == WorkspaceDiagnosticKind.Failure
                ? Contract.Severities.Error
                : Contract.Severities.Warning;

            result.Diagnostics.Add(new Contract.Diagnostic
            {
                Severity = severity,
                Code = "WORKSPACE",
                Message = e.Diagnostic.Message,
            });

            if (_options.Verbose)
            {
                _log.WriteLine($"[workspace] {e.Diagnostic.Kind}: {e.Diagnostic.Message}");
            }
        };

        var entryPoint = ResolveEntryPoint(_options.Input);
        _log.WriteLine($"[load] {entryPoint}");

        IReadOnlyList<Project> projects;
        if (entryPoint.EndsWith(".sln", StringComparison.OrdinalIgnoreCase) ||
            entryPoint.EndsWith(".slnx", StringComparison.OrdinalIgnoreCase))
        {
            var solution = await workspace.OpenSolutionAsync(entryPoint, cancellationToken: cancellationToken);
            result.SolutionPath = entryPoint;
            projects = solution.Projects.ToList();
        }
        else
        {
            // Opening one project still pulls in its project references, so the
            // call graph can cross project boundaries.
            await workspace.OpenProjectAsync(entryPoint, cancellationToken: cancellationToken);
            projects = workspace.CurrentSolution.Projects.ToList();
        }

        GroupIntoVariants(projects, result);
        await CompileAsync(result, cancellationToken);
        return result;
    }

    /// <summary>
    /// A multi-targeting project arrives as one Roslyn project per target
    /// framework, all sharing one .csproj path. Group by that path so the graph
    /// has one Project with several CompilationVariants, and pick the primary.
    /// </summary>
    private void GroupIntoVariants(IReadOnlyList<Project> projects, WorkspaceLoadResult result)
    {
        var grouped = projects
            .Where(p => p.FilePath is not null)
            .Where(p => p.Language == LanguageNames.CSharp)
            .GroupBy(p => Path.GetFullPath(p.FilePath!), StringComparer.OrdinalIgnoreCase);

        foreach (var group in grouped)
        {
            var projectPath = group.Key;
            var loaded = new LoadedProject
            {
                ProjectKey = PathUtil.ToRepoRelative(_repoRoot, projectPath),
                ProjectPath = projectPath,
            };

            var byTargetFramework = group
                .Select(p => (Project: p, TargetFramework: ReadTargetFramework(p)))
                .ToList();

            var primary = ChoosePrimary(byTargetFramework.Select(x => x.TargetFramework).ToList());

            foreach (var (project, targetFramework) in byTargetFramework)
            {
                var isPrimary = targetFramework == primary;
                if (!isPrimary && !_options.AllTargetFrameworks)
                {
                    continue;
                }

                loaded.Variants.Add(new LoadedVariant
                {
                    Project = project,
                    TargetFramework = targetFramework,
                    IsPrimary = isPrimary,
                });
            }

            if (loaded.Variants.Count > 0)
            {
                result.Projects.Add(loaded);
            }
        }
    }

    /// <summary>
    /// The target framework this Roslyn project was created for. Roslyn exposes
    /// no TFM property, so it is recovered from three places in order of how
    /// trustworthy each is.
    /// </summary>
    private static string ReadTargetFramework(Project project)
    {
        // 1. A multi-targeting load names each project "Core(net8.0)". This is
        //    MSBuild's own answer and the only one available per variant.
        var name = project.Name;
        var open = name.LastIndexOf('(');
        var close = name.LastIndexOf(')');
        if (open >= 0 && close > open)
        {
            var candidate = name[(open + 1)..close];
            if (LooksLikeTargetFramework(candidate))
            {
                return candidate;
            }
        }

        // 2. A single-target project puts the TFM in its output path
        //    (bin/Debug/net8.0/Acme.App.dll).
        var outputDirectory = project.OutputFilePath is null
            ? null
            : Path.GetFileName(Path.GetDirectoryName(project.OutputFilePath));
        if (outputDirectory is not null && LooksLikeTargetFramework(outputDirectory))
        {
            return outputDirectory;
        }

        // 3. Last resort: the framework-implied preprocessor symbols. Only the
        //    fully qualified ones identify a framework -- the bare "NET" and the
        //    "_OR_GREATER" family do not.
        var symbols = (project.ParseOptions as CSharpParseOptions)?.PreprocessorSymbolNames ?? [];
        var frameworkSymbol = symbols
            .Where(IsSpecificFrameworkSymbol)
            .Select(NormalizeFrameworkSymbol)
            .FirstOrDefault(LooksLikeTargetFramework);

        return frameworkSymbol ?? "unknown";
    }

    /// <summary>NET8_0 and NETSTANDARD2_0 identify a framework; NET, NETCOREAPP
    /// and anything _OR_GREATER do not.</summary>
    private static bool IsSpecificFrameworkSymbol(string symbol) =>
        !symbol.EndsWith("_OR_GREATER", StringComparison.Ordinal)
        && symbol.Contains('_')
        && (symbol.StartsWith("NET", StringComparison.Ordinal)
            || symbol.StartsWith("NETSTANDARD", StringComparison.Ordinal));

    /// <summary>NET8_0 -> net8.0, NETSTANDARD2_0 -> netstandard2.0.</summary>
    private static string NormalizeFrameworkSymbol(string symbol) =>
        symbol.ToLowerInvariant().Replace('_', '.');

    /// <summary>
    /// A target framework moniker: net8.0, netstandard2.0, netcoreapp3.1, net48,
    /// net8.0-windows. Guards every recovery path above so a directory called
    /// "Debug" or a bare "NET" symbol can never be stored as a TFM.
    /// </summary>
    private static bool LooksLikeTargetFramework(string candidate)
    {
        if (candidate.Length < 4)
        {
            return false;
        }

        foreach (var prefix in (string[])["netstandard", "netcoreapp", "netframework", "net"])
        {
            if (!candidate.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            var rest = candidate[prefix.Length..];
            // Must carry a version: "net" alone is not a framework, "net8.0" is.
            return rest.Length > 0 && char.IsDigit(rest[0]);
        }

        return false;
    }

    private string ChoosePrimary(List<string> targetFrameworks)
    {
        foreach (var preferred in _options.TargetFrameworkPreference)
        {
            var match = targetFrameworks.FirstOrDefault(
                tfm => string.Equals(tfm, preferred, StringComparison.OrdinalIgnoreCase));
            if (match is not null)
            {
                return match;
            }
        }

        // Nothing in the preference list matched: stay deterministic rather than
        // depending on the order MSBuild happened to return.
        return targetFrameworks.OrderBy(t => t, StringComparer.OrdinalIgnoreCase).First();
    }

    /// <summary>
    /// One Compilation per variant, built once. Call resolution is only reliable
    /// against a whole compilation, never file by file.
    /// </summary>
    private async Task CompileAsync(WorkspaceLoadResult result, CancellationToken cancellationToken)
    {
        foreach (var project in result.Projects)
        {
            foreach (var variant in project.Variants)
            {
                try
                {
                    var compilation = await variant.Project.GetCompilationAsync(cancellationToken);
                    variant.Compilation = compilation;

                    if (compilation is null)
                    {
                        variant.IsDegraded = true;
                        result.Diagnostics.Add(new Contract.Diagnostic
                        {
                            Severity = Contract.Severities.Error,
                            Code = "NO_COMPILATION",
                            Message = "Project produced no compilation.",
                            ProjectKey = project.ProjectKey,
                        });
                        continue;
                    }

                    InspectDiagnostics(project, variant, compilation, result);
                }
                catch (Exception e) when (e is not OperationCanceledException)
                {
                    variant.IsDegraded = true;
                    result.Diagnostics.Add(new Contract.Diagnostic
                    {
                        Severity = Contract.Severities.Error,
                        Code = "COMPILATION_FAILED",
                        Message = $"{e.GetType().Name}: {e.Message}",
                        ProjectKey = project.ProjectKey,
                    });
                }
            }
        }
    }

    /// <summary>
    /// Missing references are the failure that matters: without them Roslyn
    /// cannot resolve calls into those assemblies, so the scan is degraded even
    /// though it "succeeded". Ordinary code errors do not degrade it, because
    /// Roslyn still resolves most symbols in a file that fails to compile.
    /// </summary>
    private void InspectDiagnostics(
        LoadedProject project,
        LoadedVariant variant,
        Compilation compilation,
        WorkspaceLoadResult result)
    {
        var missingReferenceCodes = new[] { "CS0246", "CS0234", "CS0012", "CS0400" };
        var errors = compilation.GetDiagnostics()
            .Where(d => d.Severity == DiagnosticSeverity.Error)
            .ToList();

        variant.MissingReferenceErrorCount = errors.Count(d => missingReferenceCodes.Contains(d.Id));

        if (variant.MissingReferenceErrorCount >= _options.MissingReferenceThreshold)
        {
            variant.IsDegraded = true;
            result.Diagnostics.Add(new Contract.Diagnostic
            {
                Severity = Contract.Severities.Error,
                Code = "MISSING_REFERENCES",
                Message =
                    $"{variant.MissingReferenceErrorCount} missing-reference error(s) in " +
                    $"{project.ProjectKey} ({variant.TargetFramework}). Run 'dotnet restore' first; " +
                    "call resolution into the affected assemblies will be incomplete.",
                ProjectKey = project.ProjectKey,
            });
        }

        // Report a bounded sample of the rest so a broken build is diagnosable
        // without writing every error of a large solution into the output.
        foreach (var diagnostic in errors.Where(d => !missingReferenceCodes.Contains(d.Id)).Take(25))
        {
            var span = diagnostic.Location.GetLineSpan();
            result.Diagnostics.Add(new Contract.Diagnostic
            {
                Severity = Contract.Severities.Warning,
                Code = diagnostic.Id,
                Message = diagnostic.GetMessage(),
                ProjectKey = project.ProjectKey,
                FilePath = string.IsNullOrEmpty(span.Path) ? null : PathUtil.ToRepoRelative(_repoRoot, span.Path),
                StartLine = span.StartLinePosition.Line + 1,
            });
        }
    }

    private static string ResolveEntryPoint(string input)
    {
        if (File.Exists(input))
        {
            return input;
        }

        if (!Directory.Exists(input))
        {
            throw new FileNotFoundException($"Input path does not exist: {input}");
        }

        var solutions = Directory.GetFiles(input, "*.sln", SearchOption.TopDirectoryOnly)
            .Concat(Directory.GetFiles(input, "*.slnx", SearchOption.TopDirectoryOnly))
            .OrderBy(p => p, StringComparer.OrdinalIgnoreCase)
            .ToList();

        if (solutions.Count > 0)
        {
            return solutions[0];
        }

        var projects = Directory.GetFiles(input, "*.csproj", SearchOption.AllDirectories)
            .OrderBy(p => p, StringComparer.OrdinalIgnoreCase)
            .ToList();

        if (projects.Count == 1)
        {
            return projects[0];
        }

        if (projects.Count == 0)
        {
            throw new FileNotFoundException($"No .sln or .csproj found under {input}");
        }

        throw new InvalidOperationException(
            $"{projects.Count} projects found under {input} and no solution file. " +
            "Pass --input with a specific .sln or .csproj.");
    }
}
