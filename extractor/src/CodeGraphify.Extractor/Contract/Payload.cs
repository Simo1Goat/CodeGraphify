using System.Text.Json.Serialization;

namespace CodeGraphify.Extractor.Contract;

// These records are the C# half of the graph contract. The Python half lives in
// app/contract/models.py and is the source of truth; tests/test_contract.py
// validates this output against it, so drift fails a test rather than a scan.

public static class ContractVersion
{
    public const int SchemaVersion = 1;
}

public enum AnalysisMode
{
    Semantic,
    PartialSemantic,
    SyntaxOnly,
    Failed,
}

public static class AnalysisModeText
{
    public static string ToWire(this AnalysisMode mode) => mode switch
    {
        AnalysisMode.Semantic => "semantic",
        AnalysisMode.PartialSemantic => "partial-semantic",
        AnalysisMode.SyntaxOnly => "syntax-only",
        AnalysisMode.Failed => "failed",
        _ => throw new ArgumentOutOfRangeException(nameof(mode)),
    };
}

public sealed record SourceSpan(
    [property: JsonPropertyName("file_path")] string FilePath,
    [property: JsonPropertyName("start_line")] int StartLine,
    [property: JsonPropertyName("start_column")] int StartColumn,
    [property: JsonPropertyName("end_line")] int EndLine,
    [property: JsonPropertyName("end_column")] int EndColumn);

public sealed record Entity
{
    public required string Id { get; init; }
    public required string Kind { get; init; }
    public required string Name { get; init; }

    [JsonPropertyName("canonical_name")]
    public required string CanonicalName { get; init; }

    [JsonPropertyName("file_path")]
    public string? FilePath { get; init; }

    [JsonPropertyName("start_line")]
    public int? StartLine { get; init; }

    [JsonPropertyName("end_line")]
    public int? EndLine { get; init; }

    public Dictionary<string, object?> Metadata { get; init; } = new();
}

public sealed record Relation
{
    public required string Source { get; init; }
    public required string Target { get; init; }
    public required string Kind { get; init; }
    public required bool Resolved { get; init; }
    public SourceSpan? Evidence { get; init; }
    public Dictionary<string, object?> Metadata { get; init; } = new();
}

public sealed record CompilationContext
{
    [JsonPropertyName("variant_id")]
    public required string VariantId { get; init; }

    [JsonPropertyName("project_key")]
    public required string ProjectKey { get; init; }

    [JsonPropertyName("project_path")]
    public required string ProjectPath { get; init; }

    [JsonPropertyName("assembly_name")]
    public string? AssemblyName { get; init; }

    [JsonPropertyName("target_framework")]
    public string? TargetFramework { get; init; }

    public string Configuration { get; init; } = "Debug";

    [JsonPropertyName("language_version")]
    public string? LanguageVersion { get; init; }

    [JsonPropertyName("nullable_context")]
    public string? NullableContext { get; init; }

    [JsonPropertyName("preprocessor_symbols")]
    public string[] PreprocessorSymbols { get; init; } = [];

    [JsonPropertyName("is_primary_variant")]
    public bool IsPrimaryVariant { get; init; } = true;
}

public sealed record Diagnostic
{
    public required string Severity { get; init; }
    public required string Code { get; init; }
    public required string Message { get; init; }

    [JsonPropertyName("project_key")]
    public string? ProjectKey { get; init; }

    [JsonPropertyName("file_path")]
    public string? FilePath { get; init; }

    [JsonPropertyName("start_line")]
    public int? StartLine { get; init; }
}

public sealed record OutputFile(
    [property: JsonPropertyName("path")] string Path,
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("count")] int Count);

public sealed record Manifest
{
    [JsonPropertyName("schema_version")]
    public int SchemaVersion { get; init; } = ContractVersion.SchemaVersion;

    [JsonPropertyName("repo_id")]
    public required string RepoId { get; init; }

    [JsonPropertyName("scan_id")]
    public required string ScanId { get; init; }

    [JsonPropertyName("analysis_mode")]
    public required string AnalysisMode { get; init; }

    [JsonPropertyName("resolution_engine")]
    public string ResolutionEngine { get; init; } = "roslyn";

    [JsonPropertyName("started_at")]
    public required string StartedAt { get; init; }

    [JsonPropertyName("finished_at")]
    public required string FinishedAt { get; init; }

    [JsonPropertyName("repo_root")]
    public required string RepoRoot { get; init; }

    [JsonPropertyName("analyzed_project_keys")]
    public string[] AnalyzedProjectKeys { get; init; } = [];

    [JsonPropertyName("failed_project_keys")]
    public string[] FailedProjectKeys { get; init; } = [];

    public Dictionary<string, int> Counts { get; init; } = new();

    [JsonPropertyName("output_files")]
    public OutputFile[] OutputFiles { get; init; } = [];

    public string[] Errors { get; init; } = [];
}
