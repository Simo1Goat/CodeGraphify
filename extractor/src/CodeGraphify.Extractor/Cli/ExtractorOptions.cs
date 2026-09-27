namespace CodeGraphify.Extractor.Cli;

/// <summary>
/// The whole input side of the subprocess contract. Parsed by hand to keep the
/// extractor's dependency set to Roslyn and MSBuild only.
/// </summary>
public sealed class ExtractorOptions
{
    /// <summary>Solution or project file, or a directory to search for one.</summary>
    public required string Input { get; init; }

    public required string OutputDirectory { get; init; }

    public required string RepoId { get; init; }

    public required string ScanId { get; init; }

    /// <summary>Repo root that all emitted paths are relative to. Defaults to the
    /// git top level, else the input's directory.</summary>
    public string? RepoRoot { get; init; }

    public string Configuration { get; init; } = "Debug";

    /// <summary>
    /// Preference order for choosing the one primary variant of a multi-targeting
    /// project. First match wins; a project matching nothing falls back to its
    /// first target framework.
    /// </summary>
    public string[] TargetFrameworkPreference { get; init; } = DefaultTargetFrameworkPreference;

    public static readonly string[] DefaultTargetFrameworkPreference =
        ["net9.0", "net8.0", "net7.0", "net6.0", "netstandard2.1", "netstandard2.0", "net48", "net472"];

    /// <summary>Emit every target framework rather than one primary variant per
    /// project. Off by default: it multiplies graph size for little added insight.</summary>
    public bool AllTargetFrameworks { get; init; }

    /// <summary>
    /// A project is treated as degraded (pushing the scan to partial-semantic)
    /// once it reports this many missing-reference errors. Usually means packages
    /// were never restored.
    /// </summary>
    public int MissingReferenceThreshold { get; init; } = 1;

    public bool Verbose { get; init; }

    public static ExtractorOptions Parse(string[] args)
    {
        string? input = null;
        string? output = null;
        string? repoId = null;
        string? scanId = null;
        string? repoRoot = null;
        var configuration = "Debug";
        string[]? tfmPreference = null;
        var allTfms = false;
        var threshold = 1;
        var verbose = false;

        for (var i = 0; i < args.Length; i++)
        {
            var arg = args[i];
            switch (arg)
            {
                case "--input":
                    input = Next(args, ref i, arg);
                    break;
                case "--output":
                    output = Next(args, ref i, arg);
                    break;
                case "--repo-id":
                    repoId = Next(args, ref i, arg);
                    break;
                case "--scan-id":
                    scanId = Next(args, ref i, arg);
                    break;
                case "--repo-root":
                    repoRoot = Next(args, ref i, arg);
                    break;
                case "--configuration":
                    configuration = Next(args, ref i, arg);
                    break;
                case "--tfm-preference":
                    tfmPreference = Next(args, ref i, arg)
                        .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
                    break;
                case "--all-target-frameworks":
                    allTfms = true;
                    break;
                case "--missing-reference-threshold":
                    threshold = int.Parse(Next(args, ref i, arg));
                    break;
                case "--verbose":
                    verbose = true;
                    break;
                default:
                    throw new ArgumentException($"Unknown argument: {arg}");
            }
        }

        if (string.IsNullOrWhiteSpace(input))
        {
            throw new ArgumentException("--input is required.");
        }

        if (string.IsNullOrWhiteSpace(output))
        {
            throw new ArgumentException("--output is required.");
        }

        var resolvedInput = Path.GetFullPath(input);

        return new ExtractorOptions
        {
            Input = resolvedInput,
            OutputDirectory = Path.GetFullPath(output),
            // Default the repo id to the input's own name so ad-hoc runs need
            // fewer arguments; the orchestrator always passes one explicitly.
            RepoId = repoId ?? Path.GetFileNameWithoutExtension(resolvedInput),
            ScanId = scanId ?? Guid.NewGuid().ToString("n"),
            RepoRoot = repoRoot is null ? null : Path.GetFullPath(repoRoot),
            Configuration = configuration,
            TargetFrameworkPreference = tfmPreference ?? DefaultTargetFrameworkPreference,
            AllTargetFrameworks = allTfms,
            MissingReferenceThreshold = threshold,
            Verbose = verbose,
        };
    }

    private static string Next(string[] args, ref int i, string flag)
    {
        if (i + 1 >= args.Length)
        {
            throw new ArgumentException($"{flag} requires a value.");
        }

        return args[++i];
    }

    public const string Usage = """
        codegraphify-extractor --input <solution|project|directory> --output <dir> [options]

          --input PATH                        .sln, .csproj, or a directory to search.
          --output DIR                        Where manifest.json and the JSONL shards go.
          --repo-id ID                        Repository identity in the graph.
          --scan-id ID                        Scan identity; generated when omitted.
          --repo-root DIR                     Root that emitted paths are relative to.
          --configuration NAME                Debug (default) or Release.
          --tfm-preference a,b,c               Primary target framework preference order.
          --all-target-frameworks             Analyze every TFM, not one per project.
          --missing-reference-threshold N      Missing-reference errors before a project counts as degraded.
          --verbose                           Extra progress on stderr.

        Bulk graph data goes to the output directory as JSONL. The manifest is
        printed to stdout and also written to the output directory. Diagnostics go
        to stderr. Exit code 0 means the scan produced usable output.
        """;
}
