using CodeGraphify.Extractor.Cli;
using CodeGraphify.Extractor.Contract;
using CodeGraphify.Extractor.Output;
using CodeGraphify.Extractor.Symbols;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

namespace CodeGraphify.Extractor.Inventory;

/// <summary>
/// Turns the loaded workspace into Stage 1 output: Repository, Solution, Project,
/// CompilationVariant, File and GeneratedFile nodes, plus the structural edges
/// between them.
/// </summary>
public sealed class InventoryEmitter
{
    private readonly ExtractorOptions _options;
    private readonly string _repoRoot;
    private readonly GraphOutput _output;

    public InventoryEmitter(ExtractorOptions options, string repoRoot, GraphOutput output)
    {
        _options = options;
        _repoRoot = repoRoot;
        _output = output;
    }

    public void EmitRepository(IReadOnlyList<RepositoryFile> files, bool usedGit, string? solutionPath)
    {
        var repositoryId = EntityId.Repository(_options.RepoId);
        _output.WriteEntity(new Entity
        {
            Id = repositoryId,
            Kind = EntityKinds.Repository,
            Name = _options.RepoId,
            CanonicalName = _options.RepoId,
            Metadata = new Dictionary<string, object?>
            {
                ["inventory_source"] = usedGit ? "git" : "filesystem",
                ["file_count"] = files.Count,
            },
        });

        foreach (var file in files)
        {
            _output.WriteEntity(new Entity
            {
                Id = EntityId.File(file.RelativePath),
                Kind = EntityKinds.File,
                Name = Path.GetFileName(file.RelativePath),
                CanonicalName = file.RelativePath,
                FilePath = file.RelativePath,
                Metadata = new Dictionary<string, object?>
                {
                    ["category"] = file.Category,
                    ["generated"] = false,
                },
            });

            _output.WriteRelation(new Relation
            {
                Source = repositoryId,
                Target = EntityId.File(file.RelativePath),
                Kind = RelationKinds.Contains,
                Resolved = true,
                Metadata = BaseMetadata(),
            });
        }

        if (solutionPath is not null)
        {
            var solutionKey = PathUtil.ToRepoRelative(_repoRoot, solutionPath);
            _output.WriteEntity(new Entity
            {
                Id = EntityId.Solution(solutionKey),
                Kind = EntityKinds.Solution,
                Name = Path.GetFileNameWithoutExtension(solutionPath),
                CanonicalName = solutionKey,
                FilePath = solutionKey,
                Metadata = BaseMetadata(),
            });

            _output.WriteRelation(new Relation
            {
                Source = repositoryId,
                Target = EntityId.Solution(solutionKey),
                Kind = RelationKinds.Contains,
                Resolved = true,
                Metadata = BaseMetadata(),
            });
        }
    }

    public void EmitProjects(WorkspaceLoadResult loaded)
    {
        var repositoryId = EntityId.Repository(_options.RepoId);
        var solutionKey = loaded.SolutionPath is null
            ? null
            : PathUtil.ToRepoRelative(_repoRoot, loaded.SolutionPath);

        // Project keys of everything in this load, so project references can be
        // told apart from assembly references.
        var knownProjectPaths = loaded.Projects
            .ToDictionary(p => p.ProjectPath, p => p.ProjectKey, StringComparer.OrdinalIgnoreCase);

        foreach (var project in loaded.Projects)
        {
            var projectId = EntityId.Project(project.ProjectKey);
            _output.WriteEntity(new Entity
            {
                Id = projectId,
                Kind = EntityKinds.Project,
                Name = Path.GetFileNameWithoutExtension(project.ProjectPath),
                CanonicalName = project.ProjectKey,
                FilePath = project.ProjectKey,
                Metadata = new Dictionary<string, object?>
                {
                    ["project_key"] = project.ProjectKey,
                    ["variant_count"] = project.Variants.Count,
                    ["target_frameworks"] = project.Variants.Select(v => v.TargetFramework).ToArray(),
                    ["scan_id"] = _options.ScanId,
                    ["repo_id"] = _options.RepoId,
                },
            });

            _output.WriteRelation(new Relation
            {
                Source = solutionKey is null ? repositoryId : EntityId.Solution(solutionKey),
                Target = projectId,
                Kind = RelationKinds.Contains,
                Resolved = true,
                Metadata = BaseMetadata(),
            });

            foreach (var variant in project.Variants)
            {
                EmitVariant(project, variant, knownProjectPaths);
            }
        }
    }

    private void EmitVariant(
        LoadedProject project,
        LoadedVariant variant,
        Dictionary<string, string> knownProjectPaths)
    {
        var projectId = EntityId.Project(project.ProjectKey);
        var variantId = EntityId.Variant(project.ProjectKey, variant.TargetFramework, _options.Configuration);
        var parseOptions = variant.Project.ParseOptions as CSharpParseOptions;
        var compilationOptions = variant.Project.CompilationOptions as CSharpCompilationOptions;

        var context = new CompilationContext
        {
            VariantId = variantId,
            ProjectKey = project.ProjectKey,
            ProjectPath = project.ProjectKey,
            AssemblyName = variant.Compilation?.AssemblyName ?? variant.Project.AssemblyName,
            TargetFramework = variant.TargetFramework,
            Configuration = _options.Configuration,
            LanguageVersion = parseOptions?.LanguageVersion.ToString(),
            NullableContext = compilationOptions?.NullableContextOptions.ToString(),
            PreprocessorSymbols = parseOptions?.PreprocessorSymbolNames.ToArray() ?? [],
            IsPrimaryVariant = variant.IsPrimary,
        };

        _output.WriteCompilation(context);

        _output.WriteEntity(new Entity
        {
            Id = variantId,
            Kind = EntityKinds.CompilationVariant,
            Name = $"{Path.GetFileNameWithoutExtension(project.ProjectPath)} ({variant.TargetFramework})",
            CanonicalName = $"{project.ProjectKey}#{variant.TargetFramework}#{_options.Configuration}",
            FilePath = project.ProjectKey,
            Metadata = new Dictionary<string, object?>
            {
                ["project_key"] = project.ProjectKey,
                ["assembly_name"] = context.AssemblyName,
                ["target_framework"] = variant.TargetFramework,
                ["configuration"] = _options.Configuration,
                ["language_version"] = context.LanguageVersion,
                ["nullable_context"] = context.NullableContext,
                ["preprocessor_symbols"] = context.PreprocessorSymbols,
                ["is_primary_variant"] = variant.IsPrimary,
                ["degraded"] = variant.IsDegraded,
                ["missing_reference_errors"] = variant.MissingReferenceErrorCount,
                ["scan_id"] = _options.ScanId,
                ["repo_id"] = _options.RepoId,
            },
        });

        _output.WriteRelation(new Relation
        {
            Source = projectId,
            Target = variantId,
            Kind = RelationKinds.CompilesAs,
            Resolved = true,
            Metadata = VariantMetadata(project.ProjectKey, variant.TargetFramework),
        });

        EmitDocuments(project, variant, variantId);
        EmitReferences(project, variant, knownProjectPaths);
    }

    /// <summary>
    /// Which documents this variant actually compiles. Not the same set as the
    /// repository inventory, and not the same between variants: conditional
    /// Compile items mean a file can belong to one variant and not another.
    /// </summary>
    private void EmitDocuments(LoadedProject project, LoadedVariant variant, string variantId)
    {
        var documentPaths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        foreach (var document in variant.Project.Documents)
        {
            if (document.FilePath is not null)
            {
                documentPaths.Add(document.FilePath);
            }
        }

        foreach (var document in variant.Project.Documents)
        {
            if (document.FilePath is null)
            {
                continue;
            }

            var relativePath = PathUtil.ToRepoRelative(_repoRoot, document.FilePath);

            // A generated document (source generator output, AssemblyInfo,
            // GlobalUsings) is not in the repository inventory, so it needs its
            // own node and its own edge kind.
            var isGenerated = IsGenerated(document, relativePath);
            var fileId = EntityId.File(relativePath);

            _output.WriteEntity(new Entity
            {
                Id = fileId,
                Kind = isGenerated ? EntityKinds.GeneratedFile : EntityKinds.File,
                Name = Path.GetFileName(relativePath),
                CanonicalName = relativePath,
                FilePath = relativePath,
                Metadata = new Dictionary<string, object?>
                {
                    ["category"] = "code",
                    ["generated"] = isGenerated,
                    ["outside_repo"] = PathUtil.IsOutsideRepo(relativePath),
                },
            });

            _output.WriteRelation(new Relation
            {
                Source = variantId,
                Target = fileId,
                Kind = isGenerated ? RelationKinds.Generates : RelationKinds.Compiles,
                Resolved = true,
                Metadata = VariantMetadata(project.ProjectKey, variant.TargetFramework),
            });
        }

        EmitGeneratedTrees(project, variant, variantId, documentPaths);
    }

    /// <summary>
    /// Trees that exist only in the compilation: source generator output.
    ///
    /// These are not project documents, so the loop above never sees them -- yet
    /// declarations in them are real and stage 2 emits DECLARES edges from their file.
    /// Without a node here those edges dangle, which is how 63 of them appeared on a
    /// real ASP.NET solution the moment generators started running.
    /// </summary>
    private void EmitGeneratedTrees(
        LoadedProject project,
        LoadedVariant variant,
        string variantId,
        HashSet<string> documentPaths)
    {
        if (variant.Compilation is null)
        {
            return;
        }

        foreach (var tree in variant.Compilation.SyntaxTrees)
        {
            if (string.IsNullOrEmpty(tree.FilePath) || documentPaths.Contains(tree.FilePath))
            {
                continue;
            }

            var relativePath = PathUtil.ToRepoRelative(_repoRoot, tree.FilePath);
            var fileId = EntityId.File(relativePath);

            _output.WriteEntity(new Entity
            {
                Id = fileId,
                Kind = EntityKinds.GeneratedFile,
                Name = Path.GetFileName(relativePath),
                CanonicalName = relativePath,
                FilePath = relativePath,
                Metadata = new Dictionary<string, object?>
                {
                    ["category"] = "code",
                    ["generated"] = true,
                    ["source_generated"] = true,
                    ["outside_repo"] = true,
                },
            });

            _output.WriteRelation(new Relation
            {
                Source = variantId,
                Target = fileId,
                Kind = RelationKinds.Generates,
                Resolved = true,
                Metadata = VariantMetadata(project.ProjectKey, variant.TargetFramework),
            });
        }
    }

    private static bool IsGenerated(Document document, string relativePath)
    {
        // Roslyn does not flag MSBuild-generated documents, so recognize them by
        // where they live and what they are called.
        if (relativePath.Contains("/obj/", StringComparison.OrdinalIgnoreCase) ||
            relativePath.StartsWith("obj/", StringComparison.OrdinalIgnoreCase))
        {
            return true;
        }

        var name = Path.GetFileName(relativePath);
        return name.EndsWith(".g.cs", StringComparison.OrdinalIgnoreCase)
            || name.EndsWith(".generated.cs", StringComparison.OrdinalIgnoreCase)
            || name.EndsWith(".designer.cs", StringComparison.OrdinalIgnoreCase)
            || document.FilePath is null;
    }

    private void EmitReferences(
        LoadedProject project,
        LoadedVariant variant,
        Dictionary<string, string> knownProjectPaths)
    {
        var variantId = EntityId.Variant(project.ProjectKey, variant.TargetFramework, _options.Configuration);
        var metadata = VariantMetadata(project.ProjectKey, variant.TargetFramework);

        foreach (var reference in variant.Project.ProjectReferences)
        {
            var referenced = variant.Project.Solution.GetProject(reference.ProjectId);
            if (referenced?.FilePath is null)
            {
                continue;
            }

            var referencedKey = knownProjectPaths.TryGetValue(Path.GetFullPath(referenced.FilePath), out var known)
                ? known
                : PathUtil.ToRepoRelative(_repoRoot, referenced.FilePath);

            _output.WriteRelation(new Relation
            {
                Source = EntityId.Project(project.ProjectKey),
                Target = EntityId.Project(referencedKey),
                Kind = RelationKinds.ReferencesProject,
                Resolved = true,
                Metadata = metadata,
            });
        }

        // One node per referenced assembly, recording that the dependency exists.
        // Stage 3 hangs the external types and methods actually used off these, so
        // "we reference this library" and "we call into it" stay distinguishable.
        foreach (var reference in variant.Project.MetadataReferences.OfType<PortableExecutableReference>())
        {
            if (reference.FilePath is null)
            {
                continue;
            }

            var assemblyName = Path.GetFileNameWithoutExtension(reference.FilePath);
            var assemblyId = EntityId.ExternalSymbol($"A:{assemblyName}", assemblyName);

            _output.WriteEntity(new Entity
            {
                Id = assemblyId,
                Kind = EntityKinds.ExternalAssembly,
                Name = assemblyName,
                CanonicalName = $"A:{assemblyName}",
                Metadata = new Dictionary<string, object?>
                {
                    ["assembly_name"] = assemblyName,
                    ["external"] = true,
                },
            });

            _output.WriteRelation(new Relation
            {
                Source = variantId,
                Target = assemblyId,
                Kind = RelationKinds.ReferencesAssembly,
                Resolved = true,
                Metadata = metadata,
            });
        }
    }

    /// <summary>Fields every node and edge carries (context section 4, Stage 4).</summary>
    private Dictionary<string, object?> BaseMetadata() => new()
    {
        ["repo_id"] = _options.RepoId,
        ["scan_id"] = _options.ScanId,
        ["resolution_engine"] = "roslyn",
    };

    private Dictionary<string, object?> VariantMetadata(string projectKey, string targetFramework)
    {
        var metadata = BaseMetadata();
        metadata["project_key"] = projectKey;
        metadata["target_framework"] = targetFramework;
        metadata["configuration"] = _options.Configuration;
        return metadata;
    }
}
