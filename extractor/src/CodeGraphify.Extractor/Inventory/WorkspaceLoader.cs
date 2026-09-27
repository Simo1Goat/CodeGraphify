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

    /// <summary>Source generators this project loaded, and documents they produced.</summary>
    public int GeneratorCount { get; set; }

    public int GeneratedTreeCount { get; set; }

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

    /// <summary>
    /// Raw MSBuild failure text, kept so a failure can be attributed to the
    /// project it names. MSBuildWorkspace reports these without telling us which
    /// project they belong to, and a project MSBuild could not evaluate must not
    /// be counted as analyzed.
    /// </summary>
    public List<string> WorkspaceFailureMessages { get; } = [];

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
            // We are reading code, not auditing dependencies. NuGet's audit emits
            // NU1901-NU1904 through MSBuildWorkspace's failure channel, which is
            // pure noise here.
            ["NuGetAudit"] = "false",
        };

        using var workspace = MSBuildWorkspace.Create(properties);

        // A project that fails to load must be visible in the output, not
        // silently missing: it is the difference between semantic and
        // partial-semantic.
        workspace.WorkspaceFailed += (_, e) =>
        {
            // Collected, not reported yet. Whether a message matters depends on
            // whether the project it names ends up with a usable compilation, and
            // that is not known until every project has been compiled. Reporting
            // these as errors immediately put five benign NuGet advisories into a
            // perfectly healthy scan's output.
            result.WorkspaceFailureMessages.Add(e.Diagnostic.Message);

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
        ReportWorkspaceFailures(result);
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

        if (frameworkSymbol is not null)
        {
            return frameworkSymbol;
        }

        // 4. MSBuild failed to evaluate this project, so none of the above are
        //    populated. Read the .csproj directly rather than let "unknown" into
        //    a node key, where it would be indistinguishable between projects.
        return ReadTargetFrameworkFromProjectFile(project.FilePath) ?? "unknown";
    }

    /// <summary>
    /// The declared target framework straight out of the project XML.
    ///
    /// A deliberately shallow read: no MSBuild evaluation, no conditions, no
    /// imported props. It exists only so a project MSBuild could not load still
    /// gets a meaningful identity, and such a project is already marked degraded.
    /// The equivalent Python implementation is app/preflight.py.
    /// </summary>
    private static string? ReadTargetFrameworkFromProjectFile(string? projectFilePath)
    {
        if (projectFilePath is null || !File.Exists(projectFilePath))
        {
            return null;
        }

        try
        {
            var document = System.Xml.Linq.XDocument.Load(projectFilePath);
            var properties = document
                .Descendants()
                .Where(e => string.Equals(e.Name.LocalName, "PropertyGroup", StringComparison.OrdinalIgnoreCase))
                .SelectMany(group => group.Elements());

            string? single = null;
            string? plural = null;
            string? legacy = null;

            foreach (var property in properties)
            {
                var value = property.Value.Trim();
                if (value.Length == 0)
                {
                    continue;
                }

                switch (property.Name.LocalName.ToLowerInvariant())
                {
                    case "targetframework":
                        single ??= value;
                        break;
                    case "targetframeworks":
                        plural ??= value;
                        break;
                    case "targetframeworkversion":
                        legacy ??= value;
                        break;
                }
            }

            if (single is not null)
            {
                return single;
            }

            if (plural is not null)
            {
                // Multi-targeting: without MSBuild we cannot tell which variant
                // this project object was meant to be, so take the first.
                var first = plural.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                    .FirstOrDefault();
                if (first is not null)
                {
                    return first;
                }
            }

            // v4.8 -> net48, matching NormalizeLegacyFrameworkVersion in the probe.
            if (legacy is not null)
            {
                var digits = new string(legacy.Where(char.IsDigit).ToArray());
                return digits.Length > 0 ? $"net{digits}" : legacy;
            }

            return null;
        }
        catch (Exception e) when (e is System.Xml.XmlException or IOException or UnauthorizedAccessException)
        {
            return null;
        }
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

                    // MSBuildWorkspace loads generators but never runs them, so
                    // every type declared in a .razor or .cshtml file, and anything
                    // behind [GeneratedRegex], would be missing. Run them here.
                    var generated = SourceGeneratorRunner.Run(
                        variant.Project, compilation, cancellationToken);
                    compilation = generated.Compilation;
                    variant.Compilation = compilation;
                    variant.GeneratorCount = generated.GeneratorCount;
                    variant.GeneratedTreeCount = generated.GeneratedTreeCount;

                    if (_options.Verbose)
                    {
                        _log.WriteLine(
                            $"[generators] {project.ProjectKey} ({variant.TargetFramework}): " +
                            $"{generated.GeneratorCount} generator(s) produced " +
                            $"{generated.GeneratedTreeCount} document(s) from " +
                            $"{variant.Project.AdditionalDocuments.Count()} additional file(s)");
                    }

                    foreach (var diagnostic in generated.Diagnostics
                                 .Where(d => d.Severity >= DiagnosticSeverity.Warning)
                                 .Take(10))
                    {
                        result.Diagnostics.Add(new Contract.Diagnostic
                        {
                            Severity = Contract.Severities.Warning,
                            Code = diagnostic.Id,
                            Message = diagnostic.GetMessage(),
                            ProjectKey = project.ProjectKey,
                        });
                    }

                    if (!HasUsableCoreLibrary(compilation))
                    {
                        // No corelib means no System.Object, so nothing resolves:
                        // not a partial result, an unusable one. This is what an
                        // unrestored project or a .NET Framework project on Linux
                        // looks like from here.
                        variant.IsDegraded = true;
                        result.Diagnostics.Add(new Contract.Diagnostic
                        {
                            Severity = Contract.Severities.Error,
                            Code = "NO_CORELIB",
                            Message =
                                $"{project.ProjectKey} ({variant.TargetFramework}) compiled without a " +
                                "core library, so no symbol can be resolved. Usually an unrestored " +
                                "project, or a .NET Framework / Windows-only project being analyzed on " +
                                "a machine without the matching targeting pack.",
                            ProjectKey = project.ProjectKey,
                        });
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
    /// Whether the compilation has a core library at all.
    ///
    /// Asking Roslyn for System.Object is the reliable form of this question: a
    /// compilation without corelib hands back an error symbol, and every
    /// subsequent resolution is meaningless. Counting error codes is not
    /// equivalent -- the codes differ by what the code happens to mention.
    /// </summary>
    private static bool HasUsableCoreLibrary(Compilation compilation) =>
        compilation.GetSpecialType(SpecialType.System_Object).TypeKind != TypeKind.Error;

    /// <summary>
    /// Reports which projects MSBuild complained about, without judging them by it.
    ///
    /// MSBuildWorkspace surfaces a great deal through the same failure channel,
    /// including things that have nothing to do with whether symbols resolve: a
    /// NuGet vulnerability advisory (NU1902/NU1903) about a package version arrives
    /// here as a "failure", and so does a project in a language we do not handle.
    /// Degrading a project on that basis marked three healthy projects of a real
    /// reference application as failed, which withheld the stale sweep -- so on any
    /// repository containing a flagged package the graph would never reconcile.
    ///
    /// Whether a project is usable is decided instead by evidence about the
    /// compilation itself: does it have a core library, and can it resolve its
    /// references. Those are checked in <see cref="InspectDiagnostics"/> and
    /// <see cref="HasUsableCoreLibrary"/>, and a genuine MSBuild failure fails them
    /// anyway, because a project MSBuild could not evaluate has no references at all.
    /// </summary>
    private static void ReportWorkspaceFailures(WorkspaceLoadResult result)
    {
        if (result.WorkspaceFailureMessages.Count == 0)
        {
            return;
        }

        var attributed = new HashSet<string>(StringComparer.Ordinal);

        foreach (var project in result.Projects)
        {
            var mentions = result.WorkspaceFailureMessages
                .Where(message =>
                    message.Contains(project.ProjectPath, StringComparison.OrdinalIgnoreCase))
                .ToList();

            if (mentions.Count == 0)
            {
                continue;
            }

            var usable = project.Variants.Any(v => !v.IsDegraded);

            attributed.UnionWith(mentions);

            result.Diagnostics.Add(new Contract.Diagnostic
            {
                Severity = usable ? Contract.Severities.Warning : Contract.Severities.Error,
                Code = "MSBUILD_REPORTED",
                Message =
                    $"MSBuild reported {mentions.Count} message(s) for {project.ProjectKey}"
                    + (usable
                        ? ", but its compilation resolves normally, so it is analyzed. First: "
                        : ", and its compilation is not usable. First: ")
                    + mentions[0],
                ProjectKey = project.ProjectKey,
            });
        }

        // Messages naming no project we loaded -- a project in another language, a
        // malformed solution entry. Reported so nothing is silently swallowed, as
        // warnings because they do not affect any C# project's analysis.
        foreach (var message in result.WorkspaceFailureMessages.Where(m => !attributed.Contains(m)))
        {
            result.Diagnostics.Add(new Contract.Diagnostic
            {
                Severity = Contract.Severities.Warning,
                Code = "WORKSPACE",
                Message = message,
            });
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
        // CS0518 (predefined type not defined) is included because it is what a
        // missing core library actually produces.
        var missingReferenceCodes = new[] { "CS0246", "CS0234", "CS0012", "CS0400", "CS0518" };
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
                    $"{project.ProjectKey} ({variant.TargetFramework}); call resolution into the " +
                    "affected assemblies will be incomplete. Usual causes, in order: packages not " +
                    "restored ('dotnet restore'); a required workload not installed " +
                    "('dotnet workload restore' -- Blazor WebAssembly, MAUI and Android SDKs each " +
                    "need one); or a targeting pack for this framework missing on this machine.",
                ProjectKey = project.ProjectKey,
            });
        }

        // A bounded sample of the errors themselves, missing-reference ones included:
        // a count alone says a project is degraded without saying what is absent, and
        // "5 missing references" in a project that builds fine on the command line is
        // not something anyone can act on.
        foreach (var diagnostic in errors.Take(25))
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
