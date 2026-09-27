namespace CodeGraphify.Extractor.Contract;

/// <summary>Entity kind names. Must match app/contract/models.py EntityKind.</summary>
public static class EntityKinds
{
    public const string Repository = "Repository";
    public const string Solution = "Solution";
    public const string Project = "Project";
    public const string CompilationVariant = "CompilationVariant";
    public const string File = "File";
    public const string GeneratedFile = "GeneratedFile";
    public const string Namespace = "Namespace";
    public const string Class = "Class";
    public const string Record = "Record";
    public const string Struct = "Struct";
    public const string Interface = "Interface";
    public const string Enum = "Enum";
    public const string Delegate = "Delegate";
    public const string Method = "Method";
    public const string Constructor = "Constructor";
    public const string Property = "Property";
    public const string Field = "Field";
    public const string Event = "Event";
    public const string LocalFunction = "LocalFunction";
    public const string Route = "Route";

    /// <summary>A referenced assembly as a whole. Kept distinct from
    /// <see cref="ExternalType"/> so "which libraries do we depend on" and "which
    /// of their types do we actually touch" stay separate questions.</summary>
    public const string ExternalAssembly = "ExternalAssembly";
    public const string ExternalType = "ExternalType";
    public const string ExternalMethod = "ExternalMethod";
    public const string UnresolvedReference = "UnresolvedReference";
}

/// <summary>Relation kind names. Must match app/contract/models.py RelationKind.</summary>
public static class RelationKinds
{
    public const string Contains = "CONTAINS";
    public const string Declares = "DECLARES";
    public const string Compiles = "COMPILES";
    public const string CompilesAs = "COMPILES_AS";
    public const string Generates = "GENERATES";
    public const string ReferencesProject = "REFERENCES_PROJECT";
    public const string ReferencesAssembly = "REFERENCES_ASSEMBLY";
    public const string Inherits = "INHERITS";
    public const string Implements = "IMPLEMENTS";
    public const string Overrides = "OVERRIDES";
    public const string ImplementsMember = "IMPLEMENTS_MEMBER";
    public const string CallsDirect = "CALLS_DIRECT";
    public const string CallsVirtual = "CALLS_VIRTUAL";
    public const string CallsInterface = "CALLS_INTERFACE";

    /// <summary>A call site whose target cannot be named -- dynamic, reflective,
    /// ambiguous or broken -- pointing at an UnresolvedReference that carries the
    /// evidence. None of the CALLS_* kinds above can be used for these without
    /// asserting a dispatch that was never proved.</summary>
    public const string CallsUnresolved = "CALLS_UNRESOLVED";
    public const string MayDispatchTo = "MAY_DISPATCH_TO";
    public const string Creates = "CREATES";
    public const string Reads = "READS";
    public const string Writes = "WRITES";
    public const string RouteHandledBy = "ROUTE_HANDLED_BY";
    public const string ResolvesTo = "RESOLVES_TO";
}

/// <summary>Per-relation certainty. Must match app/contract/models.py ResolutionStatus.</summary>
public static class ResolutionStatuses
{
    public const string Resolved = "Resolved";
    public const string Ambiguous = "Ambiguous";
    public const string External = "External";
    public const string VirtualDispatch = "VirtualDispatch";
    public const string InterfaceDispatch = "InterfaceDispatch";
    public const string Dynamic = "Dynamic";
    public const string ReflectionBased = "ReflectionBased";
    public const string BuildUnavailable = "BuildUnavailable";
    public const string ParseError = "ParseError";
    public const string Unresolved = "Unresolved";
}

/// <summary>
/// How a call reaches its target, recorded separately from how confidently the
/// target was identified: a virtual call into a referenced assembly is
/// External + Virtual, and one field cannot hold both.
/// </summary>
public static class DispatchKinds
{
    public const string Direct = "Direct";
    public const string Virtual = "Virtual";
    public const string Interface = "Interface";
    public const string Dynamic = "Dynamic";
    public const string Reflection = "Reflection";
    public const string None = "None";
}

public static class Severities
{
    public const string Info = "info";
    public const string Warning = "warning";
    public const string Error = "error";
}
