using System.Collections.Immutable;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.Diagnostics;
using Microsoft.CodeAnalysis.Text;

namespace CodeGraphify.Extractor.Inventory;

/// <summary>
/// Runs a project's source generators and folds their output into the compilation.
///
/// MSBuildWorkspace loads generator references but never runs them:
/// <c>GetCompilationAsync</c> returns a compilation containing only hand-written
/// files, and <c>GetSourceGeneratedDocumentsAsync</c> returns nothing. Measured on a
/// real ASP.NET reference application: eleven generators loaded, zero documents
/// produced.
///
/// That is not a detail. Every type declared in a <c>.razor</c> or <c>.cshtml</c>
/// file is generator output, as is anything behind <c>[GeneratedRegex]</c> and much
/// dependency-injection and serializer wiring. Without running them, those types do
/// not exist, every reference to them is an error, and the project is written off as
/// having missing references -- which withholds the stale sweep for the whole scan.
///
/// The generators need their MSBuild-provided configuration to work. MSBuild writes
/// it to a generated <c>.editorconfig</c> which the workspace loads as an analyzer
/// config document, so it is parsed here and handed back to them.
/// </summary>
public static class SourceGeneratorRunner
{
    public sealed record Result(
        Compilation Compilation,
        int GeneratorCount,
        int GeneratedTreeCount,
        ImmutableArray<Diagnostic> Diagnostics);

    /// <summary>
    /// The compilation with generator output added, or the original when the project
    /// has no generators.
    ///
    /// Never throws: a generator is third-party code running in our process, and one
    /// that fails must degrade its project rather than end the scan.
    /// </summary>
    public static Result Run(Project project, Compilation compilation, CancellationToken cancellationToken)
    {
        var generators = project.AnalyzerReferences
            .SelectMany(reference => SafeGetGenerators(reference))
            .ToImmutableArray();

        if (generators.Length == 0)
        {
            return new Result(compilation, 0, 0, ImmutableArray<Diagnostic>.Empty);
        }

        // The workspace's compilation can already carry generator output from a
        // previous build, as a tree with no project document behind it. Running the
        // generators over that produces a second copy of every generated type:
        // "a partial method may not have multiple implementing declarations", and
        // every call to one becomes ambiguous. Strip it first so there is exactly one.
        var documentPaths = project.Documents
            .Select(document => document.FilePath)
            .Where(path => path is not null)
            .ToHashSet(StringComparer.OrdinalIgnoreCase)!;

        var alreadyGenerated = compilation.SyntaxTrees
            .Where(tree => !documentPaths.Contains(tree.FilePath))
            .ToList();

        if (alreadyGenerated.Count > 0)
        {
            compilation = compilation.RemoveSyntaxTrees(alreadyGenerated);
        }

        var before = compilation.SyntaxTrees.Count();

        try
        {
            var driver = CSharpGeneratorDriver.Create(
                generators,
                additionalTexts: AdditionalTexts(project),
                parseOptions: project.ParseOptions as CSharpParseOptions,
                optionsProvider: BuildOptionsProvider(project));

            driver.RunGeneratorsAndUpdateCompilation(
                compilation,
                out var updated,
                out var diagnostics,
                cancellationToken);

            var added = updated.SyntaxTrees.Count() - before;
            return new Result(updated, generators.Length, added, diagnostics);
        }
        catch (Exception e) when (e is not OperationCanceledException)
        {
            // Keep the ungenerated compilation: partial truth beats no scan, and the
            // caller reports the failure.
            return new Result(
                compilation,
                generators.Length,
                0,
                ImmutableArray.Create(GeneratorCrashed(e)));
        }
    }

    private static IEnumerable<ISourceGenerator> SafeGetGenerators(AnalyzerReference reference)
    {
        try
        {
            return reference.GetGenerators(LanguageNames.CSharp);
        }
        catch (Exception e) when (e is not OperationCanceledException)
        {
            // An analyzer assembly that cannot be loaded (built for another Roslyn
            // version) must not stop the others from running.
            return [];
        }
    }

    private static ImmutableArray<AdditionalText> AdditionalTexts(Project project)
    {
        var texts = ImmutableArray.CreateBuilder<AdditionalText>();

        foreach (var document in project.AdditionalDocuments)
        {
            if (document.FilePath is null)
            {
                continue;
            }

            try
            {
                var text = document.GetTextAsync().GetAwaiter().GetResult();
                texts.Add(new WorkspaceAdditionalText(document.FilePath, text));
            }
            catch (IOException)
            {
                // A file listed by the project but absent on disk; skip it.
            }
        }

        return texts.ToImmutable();
    }

    /// <summary>
    /// The generators' configuration, from the project's analyzer config documents.
    ///
    /// Only global configs (<c>is_global = true</c>) are read. That is the one MSBuild
    /// generates, and it carries what generators actually need:
    /// <c>build_property.*</c> for things like RootNamespace, and
    /// <c>build_metadata.AdditionalFiles.*</c> for per-file values such as the
    /// TargetPath the Razor generator uses to name a component's type. Ordinary
    /// .editorconfig files use glob sections and hold style rules, which no generator
    /// reads.
    /// </summary>
    private static AnalyzerConfigOptionsProvider BuildOptionsProvider(Project project)
    {
        var global = ImmutableDictionary.CreateBuilder<string, string>(
            StringComparer.OrdinalIgnoreCase);
        var perPath = new Dictionary<string, ImmutableDictionary<string, string>.Builder>(
            StringComparer.OrdinalIgnoreCase);

        foreach (var document in project.AnalyzerConfigDocuments)
        {
            string content;
            try
            {
                content = document.GetTextAsync().GetAwaiter().GetResult().ToString();
            }
            catch (IOException)
            {
                continue;
            }

            if (!content.Contains("is_global", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            ParseGlobalConfig(content, global, perPath);
        }

        // The Razor SDK ships a global config (RazorSourceGenerator.razorencconfig)
        // that sets this to true, and MSBuildWorkspace loads it like any other. It is
        // there so an IDE can stand the generator down because it runs Razor itself.
        // We are not an IDE: accepting it means every .razor component type is absent,
        // every reference to one is an error, and the project is written off as having
        // missing references. Measured on eShopOnWeb: eleven generators, zero output,
        // and a project that builds fine on the command line reported as failed.
        global["build_property.SuppressRazorSourceGenerator"] = "false";

        return new GeneratedConfigOptionsProvider(
            new DictionaryAnalyzerConfigOptions(global.ToImmutable()),
            perPath.ToDictionary(
                pair => pair.Key,
                pair => (AnalyzerConfigOptions)new DictionaryAnalyzerConfigOptions(pair.Value.ToImmutable()),
                StringComparer.OrdinalIgnoreCase));
    }

    private static void ParseGlobalConfig(
        string content,
        ImmutableDictionary<string, string>.Builder global,
        Dictionary<string, ImmutableDictionary<string, string>.Builder> perPath)
    {
        ImmutableDictionary<string, string>.Builder current = global;

        foreach (var rawLine in content.Split('\n'))
        {
            var line = rawLine.Trim();
            if (line.Length == 0 || line.StartsWith('#') || line.StartsWith(';'))
            {
                continue;
            }

            if (line.StartsWith('[') && line.EndsWith(']'))
            {
                // A section header in a global config is an absolute file path.
                var path = line[1..^1].Trim();
                if (!perPath.TryGetValue(path, out var builder))
                {
                    builder = ImmutableDictionary.CreateBuilder<string, string>(
                        StringComparer.OrdinalIgnoreCase);
                    perPath[path] = builder;
                }

                current = builder;
                continue;
            }

            var separator = line.IndexOf('=', StringComparison.Ordinal);
            if (separator <= 0)
            {
                continue;
            }

            var key = line[..separator].Trim();
            var value = line[(separator + 1)..].Trim();
            if (key.Length > 0)
            {
                current[key] = value;
            }
        }
    }

    private static Diagnostic GeneratorCrashed(Exception exception) => Diagnostic.Create(
        new DiagnosticDescriptor(
            "CGF0001",
            "Source generators could not be run",
            "Running source generators failed ({0}: {1}); types they declare will be missing",
            "CodeGraphify",
            DiagnosticSeverity.Warning,
            isEnabledByDefault: true),
        Location.None,
        exception.GetType().Name,
        exception.Message);

    private sealed class WorkspaceAdditionalText(string path, SourceText text) : AdditionalText
    {
        public override string Path { get; } = path;

        public override SourceText GetText(CancellationToken cancellationToken = default) => text;
    }

    private sealed class DictionaryAnalyzerConfigOptions(ImmutableDictionary<string, string> values)
        : AnalyzerConfigOptions
    {
        public override IEnumerable<string> Keys => values.Keys;

        public override bool TryGetValue(string key, out string value) =>
            values.TryGetValue(key, out value!);
    }

    private sealed class GeneratedConfigOptionsProvider(
        AnalyzerConfigOptions globalOptions,
        Dictionary<string, AnalyzerConfigOptions> byPath) : AnalyzerConfigOptionsProvider
    {
        private static readonly AnalyzerConfigOptions Empty =
            new DictionaryAnalyzerConfigOptions(ImmutableDictionary<string, string>.Empty);

        public override AnalyzerConfigOptions GlobalOptions => globalOptions;

        public override AnalyzerConfigOptions GetOptions(SyntaxTree tree) => Lookup(tree.FilePath);

        public override AnalyzerConfigOptions GetOptions(AdditionalText textFile) =>
            Lookup(textFile.Path);

        private AnalyzerConfigOptions Lookup(string? path) =>
            path is not null && byPath.TryGetValue(path, out var options) ? options : Empty;
    }
}
