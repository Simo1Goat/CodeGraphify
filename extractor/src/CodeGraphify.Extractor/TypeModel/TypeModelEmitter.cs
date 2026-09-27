using CodeGraphify.Extractor.Cli;
using CodeGraphify.Extractor.Contract;
using CodeGraphify.Extractor.Inventory;
using CodeGraphify.Extractor.Output;
using CodeGraphify.Extractor.Symbols;
using Microsoft.CodeAnalysis;

namespace CodeGraphify.Extractor.TypeModel;

/// <summary>
/// Stage 4: the type hierarchy -- INHERITS, IMPLEMENTS, OVERRIDES and
/// IMPLEMENTS_MEMBER.
///
/// This is what turns a call graph into something answerable. A
/// <c>CALLS_INTERFACE</c> edge on its own says a call went through
/// <c>IOrderService.Submit</c>; only IMPLEMENTS_MEMBER says which concrete methods
/// could have run. Phase 2's MAY_DISPATCH_TO is a traversal of these edges rather
/// than a new analysis.
/// </summary>
public sealed class TypeModelEmitter
{
    private readonly ExtractorOptions _options;
    private readonly string _repoRoot;
    private readonly GraphOutput _output;
    private readonly SymbolNodeFactory _nodes;

    public TypeModelEmitter(
        ExtractorOptions options, string repoRoot, GraphOutput output, SymbolNodeFactory nodes)
    {
        _options = options;
        _repoRoot = repoRoot;
        _output = output;
        _nodes = nodes;
    }

    public int HierarchyEdgeCount { get; private set; }

    public int MemberEdgeCount { get; private set; }

    /// <summary>
    /// Base types that every type of a given kind has whether the author wrote one
    /// or not. Emitting them would add an edge per type to a handful of universal
    /// nodes and carry no dependency information -- nobody asks which classes derive
    /// from object.
    /// </summary>
    private static readonly HashSet<SpecialType> ImplicitBaseTypes =
    [
        SpecialType.System_Object,
        SpecialType.System_ValueType,
        SpecialType.System_Enum,
        SpecialType.System_Delegate,
        SpecialType.System_MulticastDelegate,
        SpecialType.System_Array,
    ];

    public void Emit(LoadedProject project, LoadedVariant variant)
    {
        var compilation = variant.Compilation;
        if (compilation is null)
        {
            return;
        }

        var scope = new SymbolScope(compilation, project.ProjectKey, variant.TargetFramework);
        WalkNamespace(compilation.Assembly.GlobalNamespace, scope);
    }

    private void WalkNamespace(INamespaceSymbol ns, SymbolScope scope)
    {
        foreach (var nested in ns.GetNamespaceMembers())
        {
            WalkNamespace(nested, scope);
        }

        foreach (var type in ns.GetTypeMembers())
        {
            WalkType(type, scope);
        }
    }

    private void WalkType(INamedTypeSymbol type, SymbolScope scope)
    {
        if (type.IsImplicitlyDeclared)
        {
            return;
        }

        EmitBaseType(type, scope);
        EmitInterfaces(type, scope);
        EmitMemberEdges(type, scope);

        foreach (var nested in type.GetTypeMembers())
        {
            WalkType(nested, scope);
        }
    }

    // --- inheritance ---------------------------------------------------------

    private void EmitBaseType(INamedTypeSymbol type, SymbolScope scope)
    {
        var baseType = type.BaseType;
        if (baseType is null || ImplicitBaseTypes.Contains(baseType.SpecialType))
        {
            return;
        }

        var (sourceId, _) = _nodes.ResolveId(type, scope);
        var metadata = _nodes.Provenance(scope);
        metadata["resolution_status"] = ResolutionStatuses.Resolved;
        metadata["dispatch_kind"] = DispatchKinds.None;
        metadata["external_target"] = _nodes.ResolveId(baseType, scope).IsExternal;
        // The constructed form matters here: `class A : Repository<Order>` depends on
        // Repository<T>, but which T is the interesting part.
        metadata["base_type"] = SymbolKey.ConstructedSignature(baseType);

        _output.WriteRelation(new Relation
        {
            Source = sourceId,
            Target = _nodes.EnsureType(baseType, scope),
            Kind = RelationKinds.Inherits,
            Resolved = true,
            Evidence = DeclarationSpan(type),
            Metadata = metadata,
        });

        HierarchyEdgeCount++;
    }

    /// <summary>
    /// Directly declared interfaces only, not <c>AllInterfaces</c>.
    ///
    /// The transitive closure is what a graph query computes; storing it would
    /// multiply edges and, worse, make "which type actually declares this
    /// relationship" unanswerable. An interface's base interfaces are INHERITS
    /// rather than IMPLEMENTS, because an interface extends rather than implements.
    /// </summary>
    private void EmitInterfaces(INamedTypeSymbol type, SymbolScope scope)
    {
        var (sourceId, _) = _nodes.ResolveId(type, scope);
        var isInterface = type.TypeKind == TypeKind.Interface;

        foreach (var contract in type.Interfaces)
        {
            var metadata = _nodes.Provenance(scope);
            metadata["resolution_status"] = ResolutionStatuses.Resolved;
            metadata["dispatch_kind"] = DispatchKinds.None;
            metadata["external_target"] = _nodes.ResolveId(contract, scope).IsExternal;
            metadata["interface"] = SymbolKey.ConstructedSignature(contract);

            _output.WriteRelation(new Relation
            {
                Source = sourceId,
                Target = _nodes.EnsureType(contract, scope),
                Kind = isInterface ? RelationKinds.Inherits : RelationKinds.Implements,
                Resolved = true,
                Evidence = DeclarationSpan(type),
                Metadata = metadata,
            });

            HierarchyEdgeCount++;
        }
    }

    // --- members -------------------------------------------------------------

    private void EmitMemberEdges(INamedTypeSymbol type, SymbolScope scope)
    {
        foreach (var member in type.GetMembers())
        {
            if (member is INamedTypeSymbol)
            {
                continue;
            }

            EmitOverride(member, scope);
        }

        EmitInterfaceImplementations(type, scope);
    }

    private void EmitOverride(ISymbol member, SymbolScope scope)
    {
        if (!member.IsOverride)
        {
            return;
        }

        var overridden = member switch
        {
            IMethodSymbol method => (ISymbol?)method.OverriddenMethod,
            IPropertySymbol property => property.OverriddenProperty,
            IEventSymbol eventSymbol => eventSymbol.OverriddenEvent,
            _ => null,
        };

        if (overridden is null)
        {
            return;
        }

        // Accessors are not nodes, so an overriding accessor is represented by its
        // property, which carries its own OVERRIDES edge.
        if (member is IMethodSymbol { AssociatedSymbol: not null })
        {
            return;
        }

        // An override of a member of a type we do not model. Every record
        // synthesizes ToString, Equals and GetHashCode over object's, and every
        // class may override them: universal, carries no dependency information, and
        // inconsistent with leaving object out of the hierarchy in the first place.
        if (overridden.ContainingType is { } overriddenType
            && ImplicitBaseTypes.Contains(overriddenType.SpecialType))
        {
            return;
        }

        var metadata = _nodes.Provenance(scope);
        metadata["resolution_status"] = ResolutionStatuses.Resolved;
        metadata["dispatch_kind"] = DispatchKinds.None;
        metadata["overridden_signature"] = SymbolKey.Signature(overridden);
        metadata["external_target"] = _nodes.ResolveId(overridden, scope).IsExternal;

        _output.WriteRelation(new Relation
        {
            // EnsureMember, not ResolveId: an overriding member can itself be
            // synthesized, and stage 2 did not emit those.
            Source = _nodes.EnsureMember(member, scope),
            Target = _nodes.EnsureMember(overridden, scope),
            Kind = RelationKinds.Overrides,
            Resolved = true,
            Evidence = DeclarationSpan(member),
            Metadata = metadata,
        });

        MemberEdgeCount++;
    }

    /// <summary>
    /// Which member satisfies which interface member.
    ///
    /// Uses <c>FindImplementationForInterfaceMember</c> rather than matching names:
    /// an explicit implementation is not called <c>Submit</c>, and a name match would
    /// miss it entirely while also pairing up members that merely happen to agree.
    ///
    /// Walks <c>AllInterfaces</c>, so an interface inherited through a base interface
    /// is covered -- but only emits when the implementation is declared by *this*
    /// type. Without that check, a class inheriting its implementation from a base
    /// would re-emit the same edge, once per derived type, all pointing at the base's
    /// member.
    /// </summary>
    private void EmitInterfaceImplementations(INamedTypeSymbol type, SymbolScope scope)
    {
        if (type.TypeKind is TypeKind.Interface)
        {
            return;
        }

        foreach (var contract in type.AllInterfaces)
        {
            foreach (var interfaceMember in contract.GetMembers())
            {
                // Accessors are covered by their property's own edge.
                if (interfaceMember is IMethodSymbol { AssociatedSymbol: not null })
                {
                    continue;
                }

                var implementation = type.FindImplementationForInterfaceMember(interfaceMember);
                if (implementation is null)
                {
                    continue;
                }

                if (!SymbolEqualityComparer.Default.Equals(implementation.ContainingType, type))
                {
                    continue;
                }

                var isExplicit = IsExplicitImplementation(implementation, interfaceMember);

                var metadata = _nodes.Provenance(scope);
                metadata["resolution_status"] = ResolutionStatuses.Resolved;
                metadata["dispatch_kind"] = DispatchKinds.None;
                metadata["is_explicit"] = isExplicit;
                metadata["interface"] = SymbolKey.ConstructedSignature(contract);
                metadata["external_target"] = _nodes.ResolveId(interfaceMember, scope).IsExternal;

                _output.WriteRelation(new Relation
                {
                    Source = _nodes.EnsureMember(implementation, scope),
                    Target = _nodes.EnsureMember(interfaceMember, scope),
                    Kind = RelationKinds.ImplementsMember,
                    Resolved = true,
                    Evidence = DeclarationSpan(implementation),
                    Metadata = metadata,
                });

                MemberEdgeCount++;
            }
        }
    }

    private static bool IsExplicitImplementation(ISymbol implementation, ISymbol interfaceMember) =>
        implementation switch
        {
            IMethodSymbol method => method.ExplicitInterfaceImplementations.Any(
                m => SymbolEqualityComparer.Default.Equals(m, interfaceMember)),
            IPropertySymbol property => property.ExplicitInterfaceImplementations.Any(
                p => SymbolEqualityComparer.Default.Equals(p, interfaceMember)),
            IEventSymbol eventSymbol => eventSymbol.ExplicitInterfaceImplementations.Any(
                e => SymbolEqualityComparer.Default.Equals(e, interfaceMember)),
            _ => false,
        };

    // --- evidence ------------------------------------------------------------

    /// <summary>
    /// Where this relationship was declared. Ordered deterministically so a partial
    /// type does not report a different location each scan and look like it changed.
    /// </summary>
    private SourceSpan? DeclarationSpan(ISymbol symbol)
    {
        var reference = symbol.DeclaringSyntaxReferences
            .Select(r => r.SyntaxTree.GetLineSpan(r.Span))
            .Where(span => !string.IsNullOrEmpty(span.Path))
            .OrderBy(span => span.Path, StringComparer.Ordinal)
            .ThenBy(span => span.StartLinePosition.Line)
            .FirstOrDefault();

        if (string.IsNullOrEmpty(reference.Path))
        {
            // A synthesized member has no source of its own; the relationship is
            // still true, it simply has nowhere to point.
            return null;
        }

        var filePath = PathUtil.ToRepoRelative(_repoRoot, reference.Path);
        return new SourceSpan(
            filePath,
            reference.StartLinePosition.Line + 1,
            reference.StartLinePosition.Character + 1,
            reference.EndLinePosition.Line + 1,
            reference.EndLinePosition.Character + 1);
    }
}
