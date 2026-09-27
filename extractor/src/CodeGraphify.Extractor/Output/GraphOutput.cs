using System.Text.Json;
using CodeGraphify.Extractor.Contract;

namespace CodeGraphify.Extractor.Output;

/// <summary>
/// Writes the graph as JSONL shards next to a small manifest.json.
///
/// JSONL rather than one JSON array because a heavy repo produces millions of
/// rows: this streams out of the extractor and into Neo4j without either side
/// holding the whole graph in memory.
///
/// Entities and relations are de-duplicated by identity as they are written. The
/// same symbol is declared once but referenced from many files, and a partial
/// class spans several, so callers would otherwise emit duplicates.
/// </summary>
public sealed class GraphOutput : IAsyncDisposable
{
    public static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull,
        WriteIndented = false,
    };

    private readonly StreamWriter _entities;
    private readonly StreamWriter _relations;
    private readonly StreamWriter _diagnostics;
    private readonly StreamWriter _compilations;

    private readonly HashSet<string> _seenEntityIds = [];
    private readonly HashSet<string> _seenRelationKeys = [];

    public GraphOutput(string outputDirectory)
    {
        Directory.CreateDirectory(outputDirectory);
        OutputDirectory = outputDirectory;
        _entities = Create(outputDirectory, EntitiesFileName);
        _relations = Create(outputDirectory, RelationsFileName);
        _diagnostics = Create(outputDirectory, DiagnosticsFileName);
        _compilations = Create(outputDirectory, CompilationsFileName);
    }

    public const string EntitiesFileName = "entities.jsonl";
    public const string RelationsFileName = "relations.jsonl";
    public const string DiagnosticsFileName = "diagnostics.jsonl";
    public const string CompilationsFileName = "compilations.jsonl";
    public const string ManifestFileName = "manifest.json";

    public string OutputDirectory { get; }

    public int EntityCount { get; private set; }
    public int RelationCount { get; private set; }
    public int DiagnosticCount { get; private set; }
    public int CompilationCount { get; private set; }

    private static StreamWriter Create(string dir, string name) =>
        new(Path.Combine(dir, name), append: false) { AutoFlush = false };

    public void WriteEntity(Entity entity)
    {
        if (!_seenEntityIds.Add(entity.Id))
        {
            return;
        }

        _entities.WriteLine(JsonSerializer.Serialize(entity, JsonOptions));
        EntityCount++;
    }

    public void WriteRelation(Relation relation)
    {
        // One edge per call site: the same caller/target pair at a different
        // line is a distinct fact, so the evidence span is part of the key.
        var key = string.Join(
            '\u001f',
            relation.Source,
            relation.Target,
            relation.Kind,
            relation.Evidence?.FilePath ?? string.Empty,
            relation.Evidence?.StartLine.ToString() ?? string.Empty,
            relation.Evidence?.StartColumn.ToString() ?? string.Empty);

        if (!_seenRelationKeys.Add(key))
        {
            return;
        }

        _relations.WriteLine(JsonSerializer.Serialize(relation, JsonOptions));
        RelationCount++;
    }

    public void WriteDiagnostic(Contract.Diagnostic diagnostic)
    {
        _diagnostics.WriteLine(JsonSerializer.Serialize(diagnostic, JsonOptions));
        DiagnosticCount++;
    }

    public void WriteCompilation(CompilationContext context)
    {
        _compilations.WriteLine(JsonSerializer.Serialize(context, JsonOptions));
        CompilationCount++;
    }

    public OutputFile[] DescribeFiles() =>
    [
        new(EntitiesFileName, "entities", EntityCount),
        new(RelationsFileName, "relations", RelationCount),
        new(CompilationsFileName, "compilations", CompilationCount),
        new(DiagnosticsFileName, "diagnostics", DiagnosticCount),
    ];

    public async ValueTask DisposeAsync()
    {
        await _entities.FlushAsync();
        await _relations.FlushAsync();
        await _diagnostics.FlushAsync();
        await _compilations.FlushAsync();
        await _entities.DisposeAsync();
        await _relations.DisposeAsync();
        await _diagnostics.DisposeAsync();
        await _compilations.DisposeAsync();
    }
}
