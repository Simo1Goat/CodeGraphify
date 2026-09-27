using Microsoft.CodeAnalysis;

namespace CodeGraphify.Extractor.Symbols;

/// <summary>
/// Turns a Roslyn symbol into the contract's identity fields.
///
/// Canonical names come from the documentation comment id
/// (<c>M:Acme.Orders.OrderService.Submit(Acme.Orders.Order)</c>), which is the one
/// name Roslyn already guarantees is unique per symbol and stable across
/// recompiles -- unlike a display string, which varies with formatting options
/// and does not distinguish overloads reliably.
/// </summary>
public static class SymbolKey
{
    /// <summary>
    /// Signature form used for a method's metadata. Parameters are not nodes in
    /// phase 1, so this string plus the arity is what carries them.
    /// </summary>
    public static readonly SymbolDisplayFormat SignatureFormat = new(
        globalNamespaceStyle: SymbolDisplayGlobalNamespaceStyle.Omitted,
        typeQualificationStyle: SymbolDisplayTypeQualificationStyle.NameAndContainingTypesAndNamespaces,
        genericsOptions: SymbolDisplayGenericsOptions.IncludeTypeParameters,
        memberOptions: SymbolDisplayMemberOptions.IncludeParameters
            | SymbolDisplayMemberOptions.IncludeType
            | SymbolDisplayMemberOptions.IncludeContainingType,
        parameterOptions: SymbolDisplayParameterOptions.IncludeType
            | SymbolDisplayParameterOptions.IncludeName
            | SymbolDisplayParameterOptions.IncludeParamsRefOut
            | SymbolDisplayParameterOptions.IncludeDefaultValue,
        miscellaneousOptions: SymbolDisplayMiscellaneousOptions.UseSpecialTypes);

    private static readonly SymbolDisplayFormat TypeOnlyFormat = new(
        globalNamespaceStyle: SymbolDisplayGlobalNamespaceStyle.Omitted,
        typeQualificationStyle: SymbolDisplayTypeQualificationStyle.NameAndContainingTypesAndNamespaces,
        genericsOptions: SymbolDisplayGenericsOptions.IncludeTypeParameters,
        miscellaneousOptions: SymbolDisplayMiscellaneousOptions.UseSpecialTypes);

    /// <summary>
    /// The documentation comment id, or a qualified display string when Roslyn has
    /// none (local functions and some synthesized symbols). The fallback is
    /// prefixed so it can never be mistaken for a real documentation id.
    /// </summary>
    public static string CanonicalName(ISymbol symbol)
    {
        var definition = symbol.OriginalDefinition;
        var documentationId = definition.GetDocumentationCommentId();

        if (!string.IsNullOrEmpty(documentationId) && documentationId != "?")
        {
            return documentationId;
        }

        return $"X:{definition.ToDisplayString(SignatureFormat)}";
    }

    /// <summary>
    /// The node id for a symbol, following the policy in app/contract/identity.py.
    ///
    /// A symbol from the compilation being analyzed is keyed with its compilation
    /// context, because <c>#if</c> can give the same declaration a different body
    /// per target framework. A symbol from a referenced assembly is keyed by
    /// assembly alone, so every project pointing at it shares one node.
    /// </summary>
    public static string IdFor(
        ISymbol symbol,
        Compilation compilation,
        string projectKey,
        string targetFramework)
    {
        var definition = symbol.OriginalDefinition;
        var canonicalName = CanonicalName(definition);

        return IsFromThisCompilation(definition, compilation)
            ? EntityId.Symbol(canonicalName, projectKey, targetFramework)
            : EntityId.ExternalSymbol(canonicalName, definition.ContainingAssembly?.Name);
    }

    /// <summary>
    /// Whether this symbol is declared by the compilation under analysis rather
    /// than coming in through a reference.
    /// </summary>
    public static bool IsFromThisCompilation(ISymbol symbol, Compilation compilation) =>
        symbol.ContainingAssembly is not null
        && SymbolEqualityComparer.Default.Equals(symbol.ContainingAssembly, compilation.Assembly);

    public static string DisplayType(ITypeSymbol type) => type.ToDisplayString(TypeOnlyFormat);

    /// <summary>Signature of the declaration, with generics left as type
    /// parameters (<c>Repository&lt;T&gt;.Add(T item)</c>).</summary>
    public static string Signature(ISymbol symbol) =>
        symbol.OriginalDefinition.ToDisplayString(SignatureFormat);

    /// <summary>
    /// Signature as constructed at the call site
    /// (<c>Repository&lt;OrderEntity&gt;.Add(OrderEntity item)</c>).
    ///
    /// Deliberately does not normalize to OriginalDefinition: this exists to record
    /// the concrete instantiation that <see cref="Signature"/> throws away, so
    /// "who calls Repository&lt;Order&gt;.Add" stays answerable even though the edge
    /// points at Repository&lt;T&gt;.Add.
    /// </summary>
    public static string ConstructedSignature(ISymbol symbol) =>
        symbol.ToDisplayString(SignatureFormat);

    /// <summary>Entity kind for a type declaration.</summary>
    public static string EntityKindFor(INamedTypeSymbol type) => type switch
    {
        { TypeKind: TypeKind.Interface } => Contract.EntityKinds.Interface,
        { TypeKind: TypeKind.Enum } => Contract.EntityKinds.Enum,
        { TypeKind: TypeKind.Delegate } => Contract.EntityKinds.Delegate,
        // Checked before Struct/Class so `record` and `record struct` both land on
        // Record: the distinction that matters for reading a graph is that it is a
        // record, and is_value_type carries the rest.
        { IsRecord: true } => Contract.EntityKinds.Record,
        { TypeKind: TypeKind.Struct } => Contract.EntityKinds.Struct,
        _ => Contract.EntityKinds.Class,
    };

    /// <summary>Entity kind for a member declaration, or null when it is not a
    /// kind phase 1 records as a node.</summary>
    public static string? EntityKindForMember(ISymbol member) => member switch
    {
        IMethodSymbol { MethodKind: MethodKind.Constructor or MethodKind.StaticConstructor } =>
            Contract.EntityKinds.Constructor,
        IMethodSymbol => Contract.EntityKinds.Method,
        IPropertySymbol => Contract.EntityKinds.Property,
        IFieldSymbol => Contract.EntityKinds.Field,
        IEventSymbol => Contract.EntityKinds.Event,
        _ => null,
    };
}
