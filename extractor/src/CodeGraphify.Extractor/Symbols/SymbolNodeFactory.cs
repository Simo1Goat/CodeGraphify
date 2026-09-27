using CodeGraphify.Extractor.Cli;
using CodeGraphify.Extractor.Contract;
using CodeGraphify.Extractor.Output;
using Microsoft.CodeAnalysis;

namespace CodeGraphify.Extractor.Symbols;

/// <summary>The compilation a symbol is being looked at from.</summary>
public sealed record SymbolScope(Compilation Compilation, string ProjectKey, string TargetFramework);

/// <summary>
/// Turns symbols into node ids, emitting a node first when nothing else will.
///
/// Shared by stages 3 and 4 rather than copied into each. Both need to point edges
/// at types and members that stage 2 never emitted -- anything in a referenced
/// assembly, and anything the compiler synthesized -- and an edge to a node nobody
/// emitted is a dangling edge that makes Neo4j MERGE a blank node in its place.
/// A second copy of this logic has already drifted from the first once, which is
/// why there is only one.
/// </summary>
public sealed class SymbolNodeFactory
{
    private readonly ExtractorOptions _options;
    private readonly GraphOutput _output;
    private readonly AnalyzedScope _analyzedScope;

    public SymbolNodeFactory(ExtractorOptions options, GraphOutput output, AnalyzedScope analyzedScope)
    {
        _options = options;
        _output = output;
        _analyzedScope = analyzedScope;
    }

    /// <summary>
    /// The node id for a symbol, and whether it is outside the codebase.
    ///
    /// Three cases in order: declared in this compilation; declared in another
    /// project of this scan (a cross-project reference, still ours, and it must land
    /// on that project's own node rather than a duplicate); or genuinely external.
    /// </summary>
    public (string Id, bool IsExternal) ResolveId(ISymbol symbol, SymbolScope scope)
    {
        var definition = symbol.OriginalDefinition;
        var canonicalName = SymbolKey.CanonicalName(definition);

        if (SymbolKey.IsFromThisCompilation(definition, scope.Compilation))
        {
            return (EntityId.Symbol(canonicalName, scope.ProjectKey, scope.TargetFramework), false);
        }

        if (_analyzedScope.TryResolve(
                definition.ContainingAssembly?.Name, scope.TargetFramework, out var variant))
        {
            return (EntityId.Symbol(canonicalName, variant.ProjectKey, variant.TargetFramework), false);
        }

        return (EntityId.ExternalSymbol(canonicalName, definition.ContainingAssembly?.Name), true);
    }

    /// <summary>Node id for a type, emitting an ExternalType when it is not ours.</summary>
    public string EnsureType(ITypeSymbol type, SymbolScope scope)
    {
        var definition = type.OriginalDefinition;
        var (id, isExternal) = ResolveId(definition, scope);

        if (!isExternal)
        {
            return id;
        }

        var assemblyName = definition.ContainingAssembly?.Name;

        _output.WriteEntity(new Entity
        {
            Id = id,
            Kind = EntityKinds.ExternalType,
            Name = definition.Name,
            CanonicalName = SymbolKey.CanonicalName(definition),
            Metadata = new Dictionary<string, object?>
            {
                ["language"] = "csharp",
                ["external"] = true,
                ["assembly_name"] = assemblyName,
                ["full_name"] = SymbolKey.DisplayType(definition),
                ["type_kind"] = definition.TypeKind.ToString(),
            },
        });

        // Tie the type to the assembly node stage 1 created, so a dependency can be
        // traced from a reference all the way out to the library it lands in.
        if (assemblyName is not null)
        {
            _output.WriteRelation(new Relation
            {
                Source = EntityId.ExternalSymbol($"A:{assemblyName}", assemblyName),
                Target = id,
                Kind = RelationKinds.Contains,
                Resolved = true,
                Metadata = Provenance(scope),
            });
        }

        return id;
    }

    /// <summary>
    /// Node id for a member, emitting a node when stage 2 would not have.
    ///
    /// Two reasons it would not have: the member is external, or it is one stage 2
    /// deliberately skips (a delegate's Invoke, an implicit constructor). Asking
    /// stage 2's own predicate is what keeps the stages from disagreeing.
    /// </summary>
    public string EnsureMember(ISymbol member, SymbolScope scope)
    {
        var definition = member.OriginalDefinition;
        var (id, isExternal) = ResolveId(definition, scope);

        if (isExternal)
        {
            EmitExternalMember(definition, id, scope);
            return id;
        }

        if (!DeclarationEmitter.ShouldEmitMember(definition))
        {
            EmitSynthesizedMember(definition, id, scope);
        }

        return id;
    }

    private void EmitExternalMember(ISymbol member, string id, SymbolScope scope)
    {
        var metadata = Provenance(scope);
        metadata["language"] = "csharp";
        metadata["external"] = true;
        metadata["assembly_name"] = member.ContainingAssembly?.Name;
        metadata["signature"] = SymbolKey.Signature(member);
        metadata["is_static"] = member.IsStatic;

        if (member is IMethodSymbol method)
        {
            metadata["parameter_count"] = method.Parameters.Length;
            metadata["method_kind"] = method.MethodKind.ToString();
        }

        if (member.ContainingType is { } containingType)
        {
            metadata["declaring_type"] = SymbolKey.DisplayType(containingType);
        }

        _output.WriteEntity(new Entity
        {
            Id = id,
            // One kind for every external member: the graph's interest in a
            // referenced assembly is what we touch in it, not how it is structured.
            Kind = EntityKinds.ExternalMethod,
            Name = member.Name,
            CanonicalName = SymbolKey.CanonicalName(member),
            Metadata = metadata,
        });

        if (member.ContainingType is { } declaringType)
        {
            _output.WriteRelation(new Relation
            {
                Source = EnsureType(declaringType, scope),
                Target = id,
                Kind = RelationKinds.Declares,
                Resolved = true,
                Metadata = Provenance(scope),
            });
        }
    }

    /// <summary>
    /// A node for a member of ours that has no source of its own: a delegate's
    /// Invoke, an implicit constructor. Emitted on demand rather than in stage 2
    /// because only a reference to one proves it is worth having, and emitting every
    /// synthesized member of every type would bloat the graph for no gain.
    /// </summary>
    private void EmitSynthesizedMember(ISymbol member, string id, SymbolScope scope)
    {
        var kind = member switch
        {
            IMethodSymbol { MethodKind: MethodKind.Constructor or MethodKind.StaticConstructor } =>
                EntityKinds.Constructor,
            IMethodSymbol => EntityKinds.Method,
            IPropertySymbol => EntityKinds.Property,
            IEventSymbol => EntityKinds.Event,
            IFieldSymbol => EntityKinds.Field,
            _ => EntityKinds.Method,
        };

        var metadata = Provenance(scope);
        metadata["language"] = "csharp";
        metadata["external"] = false;
        metadata["synthesized"] = true;
        metadata["signature"] = SymbolKey.Signature(member);
        metadata["is_static"] = member.IsStatic;

        if (member is IMethodSymbol method)
        {
            metadata["method_kind"] = method.MethodKind.ToString();
            metadata["parameter_count"] = method.Parameters.Length;
        }

        if (member.ContainingType is { } containingType)
        {
            metadata["declaring_type"] = SymbolKey.DisplayType(containingType);
        }

        _output.WriteEntity(new Entity
        {
            Id = id,
            Kind = kind,
            Name = member.Name,
            CanonicalName = SymbolKey.CanonicalName(member),
            Metadata = metadata,
        });

        if (member.ContainingType is { } declaringType)
        {
            var (typeId, typeIsExternal) = ResolveId(declaringType, scope);
            if (!typeIsExternal)
            {
                _output.WriteRelation(new Relation
                {
                    Source = typeId,
                    Target = id,
                    Kind = RelationKinds.Declares,
                    Resolved = true,
                    Metadata = Provenance(scope),
                });
            }
        }
    }

    /// <summary>Fields every node and edge carries (context section 4, Stage 4).</summary>
    public Dictionary<string, object?> Provenance(SymbolScope scope) => new()
    {
        ["repo_id"] = _options.RepoId,
        ["scan_id"] = _options.ScanId,
        ["resolution_engine"] = "roslyn",
        ["project_key"] = scope.ProjectKey,
        ["target_framework"] = scope.TargetFramework,
        ["configuration"] = _options.Configuration,
    };
}
