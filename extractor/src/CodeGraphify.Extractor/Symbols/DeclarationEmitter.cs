using CodeGraphify.Extractor.Cli;
using CodeGraphify.Extractor.Contract;
using CodeGraphify.Extractor.Inventory;
using CodeGraphify.Extractor.Output;
using Microsoft.CodeAnalysis;

namespace CodeGraphify.Extractor.Symbols;

/// <summary>
/// Stage 2: every type and member the compilation declares.
///
/// Walks the <em>symbol</em> tree rather than the syntax trees. Both find the same
/// declarations, but the symbol tree visits each symbol exactly once, so a partial
/// class spread over several files is naturally one node with several declaring
/// references -- walking syntax would find it once per file and have to merge them
/// back together.
///
/// Containment is modelled as two edge kinds, which mean different things:
/// <list type="bullet">
/// <item><c>CONTAINS</c> -- lexical nesting: namespace in namespace, type in
/// namespace, nested type in type.</item>
/// <item><c>DECLARES</c> -- a declaration site: the type that declares a member,
/// and the file a type is declared in. A partial type gets one DECLARES edge per
/// file, which is how "where is this class defined" stays answerable.</item>
/// </list>
/// Parameters are not nodes (they would multiply node count for little analytical
/// value); a method carries its signature, arity and parameter types as metadata.
/// </summary>
public sealed class DeclarationEmitter
{
    private readonly ExtractorOptions _options;
    private readonly string _repoRoot;
    private readonly GraphOutput _output;

    public DeclarationEmitter(ExtractorOptions options, string repoRoot, GraphOutput output)
    {
        _options = options;
        _repoRoot = repoRoot;
        _output = output;
    }

    public int DeclarationCount { get; private set; }

    public void Emit(LoadedProject project, LoadedVariant variant)
    {
        var compilation = variant.Compilation;
        if (compilation is null)
        {
            return;
        }

        var context = new EmitContext(
            Compilation: compilation,
            ProjectKey: project.ProjectKey,
            TargetFramework: variant.TargetFramework,
            VariantId: EntityId.Variant(project.ProjectKey, variant.TargetFramework, _options.Configuration));

        // Assembly.GlobalNamespace, not Compilation.GlobalNamespace: the latter
        // merges in every referenced assembly's namespaces, which would emit the
        // whole framework as declarations of this project.
        WalkNamespace(compilation.Assembly.GlobalNamespace, parentNamespaceId: null, context);
    }

    private sealed record EmitContext(
        Compilation Compilation,
        string ProjectKey,
        string TargetFramework,
        string VariantId);

    private void WalkNamespace(INamespaceSymbol ns, string? parentNamespaceId, EmitContext context)
    {
        string? namespaceId = null;

        if (!ns.IsGlobalNamespace)
        {
            var name = ns.ToDisplayString();
            namespaceId = EntityId.Namespace(name);

            // Namespaces are keyed by name alone, so this node is shared between
            // projects and variants; the CONTAINS edge from the variant is what
            // records that this compilation contributes to it.
            _output.WriteEntity(new Entity
            {
                Id = namespaceId,
                Kind = EntityKinds.Namespace,
                Name = ns.Name,
                CanonicalName = SymbolKey.CanonicalName(ns),
                Metadata = new Dictionary<string, object?>
                {
                    ["language"] = "csharp",
                    ["full_name"] = name,
                },
            });

            _output.WriteRelation(new Relation
            {
                Source = parentNamespaceId ?? context.VariantId,
                Target = namespaceId,
                Kind = RelationKinds.Contains,
                Resolved = true,
                Metadata = Provenance(context),
            });
        }

        foreach (var nested in ns.GetNamespaceMembers())
        {
            WalkNamespace(nested, namespaceId, context);
        }

        foreach (var type in ns.GetTypeMembers())
        {
            // A type in the global namespace hangs off the variant, so it is still
            // reachable rather than orphaned.
            WalkType(type, namespaceId ?? context.VariantId, context);
        }
    }

    private void WalkType(INamedTypeSymbol type, string containerId, EmitContext context)
    {
        if (type.IsImplicitlyDeclared)
        {
            return;
        }

        var typeId = SymbolKey.IdFor(type, context.Compilation, context.ProjectKey, context.TargetFramework);
        var locations = DeclarationLocations(type);
        var primary = locations.FirstOrDefault();

        _output.WriteEntity(new Entity
        {
            Id = typeId,
            Kind = SymbolKey.EntityKindFor(type),
            Name = type.Name,
            CanonicalName = SymbolKey.CanonicalName(type),
            FilePath = primary?.FilePath,
            StartLine = primary?.StartLine,
            EndLine = primary?.EndLine,
            Metadata = TypeMetadata(type, context, locations),
        });

        _output.WriteRelation(new Relation
        {
            Source = containerId,
            Target = typeId,
            Kind = RelationKinds.Contains,
            Resolved = true,
            Metadata = Provenance(context),
        });

        // One edge per declaring file: a partial type is declared in each of them,
        // and the entity's own file_path can only name one.
        foreach (var location in locations)
        {
            _output.WriteRelation(new Relation
            {
                Source = EntityId.File(location.FilePath),
                Target = typeId,
                Kind = RelationKinds.Declares,
                Resolved = true,
                Evidence = location.Span,
                Metadata = Provenance(context),
            });
        }

        DeclarationCount++;

        foreach (var member in type.GetMembers())
        {
            if (member is INamedTypeSymbol nested)
            {
                WalkType(nested, typeId, context);
                continue;
            }

            EmitMember(member, typeId, context);
        }
    }

    private void EmitMember(ISymbol member, string typeId, EmitContext context)
    {
        if (!ShouldEmitMember(member))
        {
            return;
        }

        var kind = SymbolKey.EntityKindForMember(member);
        if (kind is null)
        {
            return;
        }

        var memberId = SymbolKey.IdFor(member, context.Compilation, context.ProjectKey, context.TargetFramework);
        var locations = DeclarationLocations(member);
        var primary = locations.FirstOrDefault();

        _output.WriteEntity(new Entity
        {
            Id = memberId,
            Kind = kind,
            Name = member.Name,
            CanonicalName = SymbolKey.CanonicalName(member),
            FilePath = primary?.FilePath,
            StartLine = primary?.StartLine,
            EndLine = primary?.EndLine,
            Metadata = MemberMetadata(member, context),
        });

        _output.WriteRelation(new Relation
        {
            Source = typeId,
            Target = memberId,
            Kind = RelationKinds.Declares,
            Resolved = true,
            Evidence = primary?.Span,
            Metadata = Provenance(context),
        });

        DeclarationCount++;
    }

    /// <summary>
    /// Which members become nodes. Public because stage 3 must emit a node for any
    /// target this returns false for, or its call edges dangle -- two separate
    /// predicates would drift apart, as they already did once.
    ///
    /// Property and event accessors are skipped: they are methods, but the Property
    /// or Event node already represents them, and emitting both doubles the member
    /// count while splitting calls across two nodes. Calls to an accessor are
    /// attributed to the property in stage 3.
    ///
    /// Compiler-synthesized members are skipped too, with one deliberate
    /// exception: a record's positional parameters produce properties that are
    /// implicitly declared but are unmistakably part of the type's surface, so a
    /// graph without them would be wrong.
    /// </summary>
    public static bool ShouldEmitMember(ISymbol member)
    {
        if (member is IMethodSymbol method)
        {
            if (method.AssociatedSymbol is not null)
            {
                return false;
            }

            switch (method.MethodKind)
            {
                case MethodKind.PropertyGet:
                case MethodKind.PropertySet:
                case MethodKind.EventAdd:
                case MethodKind.EventRemove:
                case MethodKind.EventRaise:
                    return false;
            }
        }

        // A backing field has no declaration of its own and no analytical value.
        if (member is IFieldSymbol { AssociatedSymbol: not null })
        {
            return false;
        }

        if (!member.IsImplicitlyDeclared)
        {
            return true;
        }

        return IsRecordPositionalProperty(member);
    }

    /// <summary>
    /// A property synthesized from a record's positional parameter. It is
    /// implicitly declared, but its declaring syntax is the parameter the author
    /// wrote, which is how it can be told apart from a truly invisible member.
    /// </summary>
    private static bool IsRecordPositionalProperty(ISymbol member) =>
        member is IPropertySymbol
        && member.ContainingType.IsRecord
        && member.DeclaringSyntaxReferences.Length > 0;

    private sealed record DeclarationLocation(string FilePath, int StartLine, int EndLine, SourceSpan Span);

    /// <summary>
    /// Every place this symbol is declared, ordered so the choice of primary
    /// location is deterministic across runs -- otherwise a partial class would
    /// report a different file each scan and look like it had changed.
    /// </summary>
    private List<DeclarationLocation> DeclarationLocations(ISymbol symbol)
    {
        var locations = new List<DeclarationLocation>();

        foreach (var reference in symbol.DeclaringSyntaxReferences)
        {
            var lineSpan = reference.SyntaxTree.GetLineSpan(reference.Span);
            if (string.IsNullOrEmpty(lineSpan.Path))
            {
                continue;
            }

            var filePath = PathUtil.ToRepoRelative(_repoRoot, lineSpan.Path);
            locations.Add(new DeclarationLocation(
                filePath,
                lineSpan.StartLinePosition.Line + 1,
                lineSpan.EndLinePosition.Line + 1,
                new SourceSpan(
                    filePath,
                    lineSpan.StartLinePosition.Line + 1,
                    lineSpan.StartLinePosition.Character + 1,
                    lineSpan.EndLinePosition.Line + 1,
                    lineSpan.EndLinePosition.Character + 1)));
        }

        return locations
            .OrderBy(l => l.FilePath, StringComparer.Ordinal)
            .ThenBy(l => l.StartLine)
            .ToList();
    }

    private Dictionary<string, object?> TypeMetadata(
        INamedTypeSymbol type,
        EmitContext context,
        List<DeclarationLocation> locations)
    {
        var metadata = Provenance(context);
        metadata["language"] = "csharp";
        metadata["assembly_name"] = context.Compilation.AssemblyName;
        metadata["namespace"] = type.ContainingNamespace.IsGlobalNamespace
            ? string.Empty
            : type.ContainingNamespace.ToDisplayString();
        metadata["accessibility"] = type.DeclaredAccessibility.ToString();
        metadata["is_static"] = type.IsStatic;
        metadata["is_abstract"] = type.IsAbstract;
        metadata["is_sealed"] = type.IsSealed;
        metadata["is_value_type"] = type.IsValueType;
        metadata["is_record"] = type.IsRecord;
        metadata["is_generic"] = type.IsGenericType;
        metadata["type_parameter_count"] = type.Arity;
        metadata["external"] = false;
        metadata["generated"] = locations.Any(l => IsGeneratedPath(l.FilePath));

        // A partial type is one node declared in several files; both facts have to
        // be on the node, because the single file_path cannot express it.
        metadata["is_partial"] = locations.Count > 1;
        if (locations.Count > 1)
        {
            metadata["declaration_files"] = locations.Select(l => l.FilePath).Distinct().ToArray();
        }

        if (type.TypeKind == TypeKind.Enum && type.EnumUnderlyingType is not null)
        {
            metadata["enum_underlying_type"] = SymbolKey.DisplayType(type.EnumUnderlyingType);
        }

        return metadata;
    }

    private Dictionary<string, object?> MemberMetadata(ISymbol member, EmitContext context)
    {
        var metadata = Provenance(context);
        metadata["language"] = "csharp";
        metadata["assembly_name"] = context.Compilation.AssemblyName;
        metadata["declaring_type"] = SymbolKey.DisplayType(member.ContainingType);
        metadata["accessibility"] = member.DeclaredAccessibility.ToString();
        metadata["is_static"] = member.IsStatic;
        metadata["is_abstract"] = member.IsAbstract;
        metadata["is_virtual"] = member.IsVirtual;
        metadata["is_override"] = member.IsOverride;
        metadata["is_sealed"] = member.IsSealed;
        metadata["external"] = false;
        metadata["signature"] = SymbolKey.Signature(member);

        switch (member)
        {
            case IMethodSymbol method:
                // Parameters are metadata, not nodes: enough to tell overloads
                // apart and to read a signature, without a node per parameter.
                metadata["parameter_count"] = method.Parameters.Length;
                metadata["parameter_types"] = method.Parameters
                    .Select(p => SymbolKey.DisplayType(p.Type))
                    .ToArray();
                metadata["type_parameter_count"] = method.Arity;
                metadata["return_type"] = SymbolKey.DisplayType(method.ReturnType);
                metadata["is_async"] = method.IsAsync;
                metadata["is_extension_method"] = method.IsExtensionMethod;
                metadata["is_generic"] = method.IsGenericMethod;
                metadata["method_kind"] = method.MethodKind.ToString();
                break;

            case IPropertySymbol property:
                metadata["property_type"] = SymbolKey.DisplayType(property.Type);
                metadata["is_indexer"] = property.IsIndexer;
                metadata["is_read_only"] = property.IsReadOnly;
                metadata["is_write_only"] = property.IsWriteOnly;
                metadata["is_required"] = property.IsRequired;
                break;

            case IFieldSymbol field:
                metadata["field_type"] = SymbolKey.DisplayType(field.Type);
                metadata["is_const"] = field.IsConst;
                metadata["is_read_only"] = field.IsReadOnly;
                break;

            case IEventSymbol eventSymbol:
                metadata["event_type"] = SymbolKey.DisplayType(eventSymbol.Type);
                break;
        }

        return metadata;
    }

    private static bool IsGeneratedPath(string filePath) =>
        filePath.Contains("/obj/", StringComparison.OrdinalIgnoreCase)
        || filePath.EndsWith(".g.cs", StringComparison.OrdinalIgnoreCase)
        || filePath.EndsWith(".generated.cs", StringComparison.OrdinalIgnoreCase);

    /// <summary>Fields every node and edge carries (context section 4, Stage 4).</summary>
    private Dictionary<string, object?> Provenance(EmitContext context) => new()
    {
        ["repo_id"] = _options.RepoId,
        ["scan_id"] = _options.ScanId,
        ["resolution_engine"] = "roslyn",
        ["project_key"] = context.ProjectKey,
        ["target_framework"] = context.TargetFramework,
        ["configuration"] = _options.Configuration,
        ["analysis_mode_hint"] = "semantic",
    };
}
