using CodeGraphify.Extractor.Cli;
using CodeGraphify.Extractor.Contract;
using CodeGraphify.Extractor.Inventory;
using CodeGraphify.Extractor.Output;
using CodeGraphify.Extractor.Symbols;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Operations;

namespace CodeGraphify.Extractor.Calls;

/// <summary>
/// Stage 3: who calls what.
///
/// Driven by the operation API (<see cref="IInvocationOperation"/> and friends)
/// rather than by <c>GetSymbolInfo</c> on invocation syntax. The operation tree
/// distinguishes cases that look identical in syntax -- a dynamic invocation, a
/// delegate invoke, a reduced extension method call, an invalid call with
/// candidates -- so the classification below can be made on what Roslyn actually
/// proved rather than on what the source looks like.
///
/// Two fields describe every call edge, and they answer different questions:
/// <c>dispatch_kind</c> is how the call reaches its target, <c>resolution_status</c>
/// is how confidently the target was identified. A virtual call into a referenced
/// assembly is External + Virtual; collapsing those into one field loses half of it.
///
/// Nothing is ever guessed. A call whose target cannot be named becomes a
/// CALLS_UNRESOLVED edge to an UnresolvedReference node carrying the source
/// evidence, so the uncertainty is in the graph instead of being papered over.
/// </summary>
public sealed class CallEmitter
{
    private readonly ExtractorOptions _options;
    private readonly string _repoRoot;
    private readonly GraphOutput _output;
    private readonly AnalyzedScope _scope;

    public CallEmitter(
        ExtractorOptions options, string repoRoot, GraphOutput output, AnalyzedScope scope)
    {
        _options = options;
        _repoRoot = repoRoot;
        _output = output;
        _scope = scope;
    }

    public int CallCount { get; private set; }

    public int UnresolvedCount { get; private set; }

    /// <summary>
    /// Reflection entry points, as "ContainingType.MethodName". A call to one of
    /// these means some other method is invoked here that Roslyn cannot name, so it
    /// gets a ReflectionBased edge in addition to the ordinary call edge to the
    /// reflection API itself.
    /// </summary>
    private static readonly HashSet<string> ReflectionEntryPoints = new(StringComparer.Ordinal)
    {
        "System.Reflection.MethodBase.Invoke",
        "System.Reflection.MethodInfo.Invoke",
        "System.Reflection.ConstructorInfo.Invoke",
        "System.Activator.CreateInstance",
        "System.Type.InvokeMember",
        "System.Reflection.Assembly.CreateInstance",
        "System.Reflection.PropertyInfo.GetValue",
        "System.Reflection.PropertyInfo.SetValue",
        "System.Reflection.FieldInfo.GetValue",
        "System.Reflection.FieldInfo.SetValue",
    };

    private sealed record CallContext(
        Compilation Compilation,
        string ProjectKey,
        string TargetFramework,
        bool TreeHasErrors);

    public void Emit(LoadedProject project, LoadedVariant variant)
    {
        var compilation = variant.Compilation;
        if (compilation is null)
        {
            return;
        }

        foreach (var tree in compilation.SyntaxTrees)
        {
            var model = compilation.GetSemanticModel(tree);
            var context = new CallContext(
                compilation,
                project.ProjectKey,
                variant.TargetFramework,
                TreeHasErrors: tree.GetDiagnostics().Any(d => d.Severity == DiagnosticSeverity.Error));

            foreach (var node in tree.GetRoot().DescendantNodes())
            {
                switch (node)
                {
                    case InvocationExpressionSyntax invocation:
                        EmitInvocation(invocation, model, context);
                        break;

                    case BaseObjectCreationExpressionSyntax creation:
                        EmitObjectCreation(creation, model, context);
                        break;

                    // A method group used as a delegate: not a call, a reference.
                    case IdentifierNameSyntax or MemberAccessExpressionSyntax:
                        EmitMethodReference(node, model, context);
                        break;
                }
            }
        }
    }

    // --- invocations ---------------------------------------------------------

    private void EmitInvocation(
        InvocationExpressionSyntax invocation,
        SemanticModel model,
        CallContext context)
    {
        var caller = CallerResolver.Find(invocation, model);
        if (caller is null)
        {
            return;
        }

        var operation = model.GetOperation(invocation);
        var evidence = Span(invocation);

        switch (operation)
        {
            case IInvocationOperation invocationOperation:
                EmitResolvedCall(caller, invocationOperation, invocation, evidence, context);
                break;

            // Invocation-shaped syntax that is not a call at all: nameof(x) is a
            // compile-time constant. Reporting these as unresolved calls would fill
            // the Unresolved bucket with noise and make the real ones worthless.
            case INameOfOperation:
            case ITypeOfOperation:
            case ISizeOfOperation:
            case IDefaultValueOperation:
                return;

            // A call through a function pointer: the target genuinely is not known
            // statically, so it is unresolved for an honest reason.
            case IFunctionPointerInvocationOperation:
                EmitUnresolved(
                    caller,
                    invocation,
                    evidence,
                    context,
                    ResolutionStatuses.Unresolved,
                    DispatchKinds.None,
                    "function pointer invocation: the target is not known statically");
                break;

            // `dynamic` defers everything to run time: Roslyn has no target to name.
            case IDynamicInvocationOperation:
                EmitUnresolved(
                    caller,
                    invocation,
                    evidence,
                    context,
                    ResolutionStatuses.Dynamic,
                    DispatchKinds.Dynamic,
                    "dynamic dispatch: the target is chosen at run time");
                break;

            default:
                EmitUnresolvedFromSymbolInfo(caller, invocation, evidence, context);
                break;
        }
    }

    private void EmitResolvedCall(
        ISymbol caller,
        IInvocationOperation operation,
        InvocationExpressionSyntax invocation,
        SourceSpan evidence,
        CallContext context)
    {
        // Normalize to the declaration that was actually written:
        //   ReducedFrom  -- repo.AddAll(x) is really AddAll(repo, x)
        //   OriginalDefinition -- Repository<Order>.Add collapses to Repository<T>.Add
        // Without this, every generic instantiation becomes its own node and the
        // call graph fragments into targets nobody declared.
        // The operation API hands back the *unreduced* static method for an
        // extension call -- one of the reasons context section 3 prefers it over
        // GetSymbolInfo on invocation syntax. ReducedFrom is still consulted, for
        // the paths where a reduced symbol does arrive.
        var target = operation.TargetMethod;
        var reduced = target.ReducedFrom;
        if (reduced is not null)
        {
            target = reduced;
        }

        // Repository<Order>.Add collapses to Repository<T>.Add. Without this every
        // instantiation becomes its own node and the call graph fragments into
        // targets nobody declared.
        target = target.OriginalDefinition;

        // A local function is not a node in phase 1. Dropping the edge loses
        // nothing real: calls made *inside* the local function are already
        // attributed to the member containing it, so its body is represented --
        // only the hop through its name would be, and that would dangle.
        if (target.MethodKind == MethodKind.LocalFunction)
        {
            return;
        }

        // Stage 2 emits the Property or Event, not get_X / add_X, so a call to an
        // accessor belongs to the member it serves.
        if (target.AssociatedSymbol is { } associated)
        {
            EmitAccessorCall(caller, associated, operation, evidence, context);
            return;
        }

        var (kind, dispatch) = Classify(operation, target);
        var targetId = EnsureTarget(target, context);
        var isExternal = ResolveId(target, context).IsExternal;
        var status = isExternal ? ResolutionStatuses.External : DispatchStatus(dispatch);

        var metadata = Provenance(context);
        metadata["resolution_status"] = status;
        metadata["dispatch_kind"] = dispatch;
        metadata["target_signature"] = SymbolKey.Signature(target);
        metadata["external"] = isExternal;

        if (target.IsExtensionMethod)
        {
            metadata["is_extension_method"] = true;
        }

        if (!SymbolEqualityComparer.Default.Equals(
                operation.TargetMethod, operation.TargetMethod.OriginalDefinition))
        {
            // Keep the constructed form so "who calls Repository<Order>.Add" is
            // still answerable even though the edge points at Repository<T>.Add.
            metadata["constructed_target"] = SymbolKey.ConstructedSignature(operation.TargetMethod);
        }

        _output.WriteRelation(new Relation
        {
            Source = CallerId(caller, context),
            Target = targetId,
            Kind = kind,
            Resolved = true,
            Evidence = evidence,
            Metadata = metadata,
        });

        CallCount++;

        if (IsReflectionEntryPoint(target))
        {
            // The call to Invoke itself is resolved; what it invokes is not.
            EmitUnresolved(
                caller,
                invocation,
                evidence,
                context,
                ResolutionStatuses.ReflectionBased,
                DispatchKinds.Reflection,
                $"reflective invocation via {target.ContainingType.ToDisplayString()}.{target.Name}");
        }
    }

    /// <summary>
    /// A call that goes through a property or event accessor, recorded against the
    /// Property or Event node. The dispatch classification still comes from the
    /// accessor, because that is what virtual-ness applies to.
    /// </summary>
    private void EmitAccessorCall(
        ISymbol caller,
        ISymbol associated,
        IInvocationOperation operation,
        SourceSpan evidence,
        CallContext context)
    {
        var definition = associated.OriginalDefinition;
        var (targetId, isExternal) = ResolveId(definition, context);

        var dispatch = definition.ContainingType?.TypeKind == TypeKind.Interface
            ? DispatchKinds.Interface
            : operation.IsVirtual
                ? DispatchKinds.Virtual
                : DispatchKinds.Direct;

        var kind = dispatch switch
        {
            DispatchKinds.Interface => RelationKinds.CallsInterface,
            DispatchKinds.Virtual => RelationKinds.CallsVirtual,
            _ => RelationKinds.CallsDirect,
        };

        if (isExternal)
        {
            EnsureExternalMember(definition, targetId, context);
        }

        var metadata = Provenance(context);
        metadata["resolution_status"] = isExternal
            ? ResolutionStatuses.External
            : DispatchStatus(dispatch);
        metadata["dispatch_kind"] = dispatch;
        metadata["external"] = isExternal;
        metadata["via_accessor"] = operation.TargetMethod.Name;

        _output.WriteRelation(new Relation
        {
            Source = CallerId(caller, context),
            Target = targetId,
            Kind = kind,
            Resolved = true,
            Evidence = evidence,
            Metadata = metadata,
        });

        CallCount++;
    }

    /// <summary>A minimal node for an external non-method member reached by a call
    /// (an external property or event through its accessor).</summary>
    private void EnsureExternalMember(ISymbol member, string id, CallContext context)
    {
        var metadata = Provenance(context);
        metadata["language"] = "csharp";
        metadata["external"] = true;
        metadata["assembly_name"] = member.ContainingAssembly?.Name;
        metadata["signature"] = SymbolKey.Signature(member);
        if (member.ContainingType is { } containingType)
        {
            metadata["declaring_type"] = SymbolKey.DisplayType(containingType);
        }

        _output.WriteEntity(new Entity
        {
            Id = id,
            Kind = EntityKinds.ExternalMethod,
            Name = member.Name,
            CanonicalName = SymbolKey.CanonicalName(member),
            Metadata = metadata,
        });

        if (member.ContainingType is { } declaringType)
        {
            _output.WriteRelation(new Relation
            {
                Source = EnsureType(declaringType, context),
                Target = id,
                Kind = RelationKinds.Declares,
                Resolved = true,
                Metadata = Provenance(context),
            });
        }
    }

    /// <summary>
    /// Which of the three call edges this is.
    ///
    /// Interface first, because a call through an interface-typed receiver is an
    /// interface dispatch whatever else is true of it. Then
    /// <see cref="IInvocationOperation.IsVirtual"/>, which is Roslyn's own answer and
    /// correctly reports <c>base.M()</c> as non-virtual -- <c>base</c> suppresses
    /// virtual dispatch, so it is a direct call even though the member is virtual.
    /// Checking <c>IsVirtual</c> on the *symbol* instead would get that wrong.
    /// </summary>
    private static (string Kind, string Dispatch) Classify(
        IInvocationOperation operation, IMethodSymbol target)
    {
        if (target.ContainingType?.TypeKind == TypeKind.Interface)
        {
            return (RelationKinds.CallsInterface, DispatchKinds.Interface);
        }

        return operation.IsVirtual
            ? (RelationKinds.CallsVirtual, DispatchKinds.Virtual)
            : (RelationKinds.CallsDirect, DispatchKinds.Direct);
    }

    private static string DispatchStatus(string dispatch) => dispatch switch
    {
        DispatchKinds.Interface => ResolutionStatuses.InterfaceDispatch,
        DispatchKinds.Virtual => ResolutionStatuses.VirtualDispatch,
        _ => ResolutionStatuses.Resolved,
    };

    // --- object creation -----------------------------------------------------

    private void EmitObjectCreation(
        BaseObjectCreationExpressionSyntax creation,
        SemanticModel model,
        CallContext context)
    {
        var caller = CallerResolver.Find(creation, model);
        if (caller is null)
        {
            return;
        }

        var evidence = Span(creation);

        if (model.GetOperation(creation) is not IObjectCreationOperation operation)
        {
            // `new dynamic(...)` and broken creations land here.
            EmitUnresolvedFromSymbolInfo(caller, creation, evidence, context);
            return;
        }

        var createdType = operation.Type?.OriginalDefinition;
        if (createdType is null)
        {
            return;
        }

        var callerId = CallerId(caller, context);
        var isExternal = ResolveId(createdType, context).IsExternal;

        var metadata = Provenance(context);
        metadata["resolution_status"] = isExternal
            ? ResolutionStatuses.External
            : ResolutionStatuses.Resolved;
        metadata["dispatch_kind"] = DispatchKinds.Direct;
        metadata["external"] = isExternal;
        metadata["created_type"] = SymbolKey.DisplayType((ITypeSymbol)createdType);

        // CREATES points at the type, which is what dependency analysis asks about
        // ("who instantiates OrderService").
        _output.WriteRelation(new Relation
        {
            Source = callerId,
            Target = EnsureType((ITypeSymbol)createdType, context),
            Kind = RelationKinds.Creates,
            Resolved = true,
            Evidence = evidence,
            Metadata = metadata,
        });

        // The constructor call is a separate fact, and matters when the constructor
        // has behaviour of its own.
        if (operation.Constructor is { } constructor)
        {
            var constructorTarget = constructor.OriginalDefinition;
            var constructorMetadata = Provenance(context);
            constructorMetadata["resolution_status"] =
                ResolveId(constructorTarget, context).IsExternal
                    ? ResolutionStatuses.External
                    : ResolutionStatuses.Resolved;
            constructorMetadata["dispatch_kind"] = DispatchKinds.Direct;
            constructorMetadata["target_signature"] = SymbolKey.Signature(constructorTarget);

            _output.WriteRelation(new Relation
            {
                Source = callerId,
                Target = EnsureTarget(constructorTarget, context),
                Kind = RelationKinds.CallsDirect,
                Resolved = true,
                Evidence = evidence,
                Metadata = constructorMetadata,
            });
        }

        CallCount++;
    }

    // --- method groups -------------------------------------------------------

    /// <summary>
    /// A method used as a value rather than called: <c>handler = OnSubmitted</c> or
    /// <c>Select(Describe)</c>. RESOLVES_TO, not a call -- the method may never run,
    /// and recording it as a call would invent control flow that does not exist.
    /// </summary>
    private void EmitMethodReference(SyntaxNode node, SemanticModel model, CallContext context)
    {
        // Only the outermost node of a member access should be considered, and never
        // the callee position of an invocation (that is the call itself).
        if (node.Parent is InvocationExpressionSyntax invocation && invocation.Expression == node)
        {
            return;
        }

        if (node.Parent is MemberAccessExpressionSyntax parentAccess && parentAccess.Name == node)
        {
            return;
        }

        if (model.GetOperation(node) is not IMethodReferenceOperation reference)
        {
            return;
        }

        var caller = CallerResolver.Find(node, model);
        if (caller is null)
        {
            return;
        }

        var target = reference.Method.OriginalDefinition;
        if (target.MethodKind == MethodKind.LocalFunction)
        {
            return;
        }

        var metadata = Provenance(context);
        metadata["resolution_status"] = ResolveId(target, context).IsExternal
            ? ResolutionStatuses.External
            : ResolutionStatuses.Resolved;
        metadata["dispatch_kind"] = DispatchKinds.None;
        metadata["target_signature"] = SymbolKey.Signature(target);
        metadata["reference_only"] = true;

        _output.WriteRelation(new Relation
        {
            Source = CallerId(caller, context),
            Target = EnsureTarget(target, context),
            Kind = RelationKinds.ResolvesTo,
            Resolved = true,
            Evidence = Span(node),
            Metadata = metadata,
        });
    }

    // --- unresolved ----------------------------------------------------------

    /// <summary>
    /// A call the operation API could not turn into a target. Consults
    /// <c>GetSymbolInfo</c> for candidate symbols, which is what distinguishes
    /// "several overloads matched" from "nothing matched at all".
    /// </summary>
    private void EmitUnresolvedFromSymbolInfo(
        ISymbol caller,
        ExpressionSyntax expression,
        SourceSpan evidence,
        CallContext context)
    {
        var symbolInfo = expression is InvocationExpressionSyntax invocation
            ? context.Compilation.GetSemanticModel(expression.SyntaxTree).GetSymbolInfo(invocation.Expression)
            : context.Compilation.GetSemanticModel(expression.SyntaxTree).GetSymbolInfo(expression);

        var candidates = symbolInfo.CandidateSymbols;

        // A syntax error means the call may be fine and merely unparseable; saying
        // Unresolved would blame the code rather than the state of the file.
        var status = candidates.Length > 0
            ? ResolutionStatuses.Ambiguous
            : context.TreeHasErrors
                ? ResolutionStatuses.ParseError
                : ResolutionStatuses.Unresolved;

        var detail = candidates.Length > 0
            ? $"{candidates.Length} candidate(s), none chosen: {symbolInfo.CandidateReason}"
            : "no symbol could be resolved for this call";

        EmitUnresolved(
            caller,
            expression,
            evidence,
            context,
            status,
            DispatchKinds.None,
            detail,
            candidates);
    }

    private void EmitUnresolved(
        ISymbol caller,
        ExpressionSyntax expression,
        SourceSpan evidence,
        CallContext context,
        string status,
        string dispatch,
        string detail,
        IReadOnlyList<ISymbol>? candidates = null)
    {
        var text = Truncate(expression.ToString());
        var unresolvedId = EntityId.Unresolved(
            evidence.FilePath, evidence.StartLine, evidence.StartColumn, text);

        var entityMetadata = Provenance(context);
        entityMetadata["expression"] = text;
        entityMetadata["resolution_status"] = status;
        entityMetadata["dispatch_kind"] = dispatch;
        entityMetadata["detail"] = detail;
        entityMetadata["external"] = false;

        if (candidates is { Count: > 0 })
        {
            entityMetadata["candidates"] = candidates
                .Select(SymbolKey.CanonicalName)
                .Distinct(StringComparer.Ordinal)
                .Take(20)
                .ToArray();
        }

        _output.WriteEntity(new Entity
        {
            Id = unresolvedId,
            Kind = EntityKinds.UnresolvedReference,
            Name = text,
            // Identity is the call site, because there is no symbol to name.
            CanonicalName =
                $"U:{evidence.FilePath}:{evidence.StartLine}:{evidence.StartColumn}:{text}",
            FilePath = evidence.FilePath,
            StartLine = evidence.StartLine,
            EndLine = evidence.EndLine,
            Metadata = entityMetadata,
        });

        var relationMetadata = Provenance(context);
        relationMetadata["resolution_status"] = status;
        relationMetadata["dispatch_kind"] = dispatch;
        relationMetadata["detail"] = detail;

        _output.WriteRelation(new Relation
        {
            Source = CallerId(caller, context),
            Target = unresolvedId,
            Kind = RelationKinds.CallsUnresolved,
            // The call is real; the target is not known. `resolved: false` is the
            // field that says so.
            Resolved = false,
            Evidence = evidence,
            Metadata = relationMetadata,
        });

        UnresolvedCount++;
    }

    // --- target identity -----------------------------------------------------

    /// <summary>
    /// The node id for a call target, and whether it is outside the codebase.
    ///
    /// Three cases, in order: declared in this compilation; declared in another
    /// project of this scan (a cross-project call, still ours, and it must land on
    /// that project's own node rather than a duplicate); or genuinely external.
    /// </summary>
    private (string Id, bool IsExternal) ResolveId(ISymbol symbol, CallContext context)
    {
        var definition = symbol.OriginalDefinition;
        var canonicalName = SymbolKey.CanonicalName(definition);

        if (SymbolKey.IsFromThisCompilation(definition, context.Compilation))
        {
            return (EntityId.Symbol(canonicalName, context.ProjectKey, context.TargetFramework), false);
        }

        if (_scope.TryResolve(definition.ContainingAssembly?.Name, context.TargetFramework, out var variant))
        {
            return (EntityId.Symbol(canonicalName, variant.ProjectKey, variant.TargetFramework), false);
        }

        return (EntityId.ExternalSymbol(canonicalName, definition.ContainingAssembly?.Name), true);
    }

    // --- external symbols ----------------------------------------------------

    /// <summary>
    /// The node id for a call target, emitting an ExternalMethod (and the
    /// ExternalType that declares it) when the target lives in a referenced
    /// assembly. Internal targets already have nodes from stage 2.
    /// </summary>
    private string EnsureTarget(IMethodSymbol target, CallContext context)
    {
        var (id, isExternal) = ResolveId(target, context);

        if (!isExternal)
        {
            // Anything stage 2 chose not to emit -- a delegate's Invoke, an
            // implicit default constructor -- needs a node here, or the edge
            // dangles and Neo4j MERGEs a blank node in its place. Asking stage 2's
            // own predicate is what keeps the two stages from disagreeing.
            if (!Symbols.DeclarationEmitter.ShouldEmitMember(target))
            {
                EmitSynthesizedMember(target, id, context);
            }

            return id;
        }

        var assemblyName = target.ContainingAssembly?.Name;

        _output.WriteEntity(new Entity
        {
            Id = id,
            Kind = EntityKinds.ExternalMethod,
            Name = target.Name,
            CanonicalName = SymbolKey.CanonicalName(target),
            Metadata = new Dictionary<string, object?>
            {
                ["language"] = "csharp",
                ["external"] = true,
                ["assembly_name"] = assemblyName,
                ["declaring_type"] = target.ContainingType is null
                    ? null
                    : SymbolKey.DisplayType(target.ContainingType),
                ["signature"] = SymbolKey.Signature(target),
                ["parameter_count"] = target.Parameters.Length,
                ["is_static"] = target.IsStatic,
            },
        });

        if (target.ContainingType is { } containingType)
        {
            var typeId = EnsureType(containingType, context);
            _output.WriteRelation(new Relation
            {
                Source = typeId,
                Target = id,
                Kind = RelationKinds.Declares,
                Resolved = true,
                Metadata = Provenance(context),
            });
        }

        return id;
    }

    /// <summary>
    /// A node for a member of ours that has no source of its own: a delegate's
    /// Invoke, a synthesized record method. Emitted here rather than in stage 2
    /// because only a call site proves one is worth having, and emitting every
    /// synthesized member of every type would bloat the graph for no gain.
    /// </summary>
    private void EmitSynthesizedMember(IMethodSymbol target, string id, CallContext context)
    {
        var kind = target.MethodKind is MethodKind.Constructor or MethodKind.StaticConstructor
            ? EntityKinds.Constructor
            : EntityKinds.Method;

        var metadata = Provenance(context);
        metadata["language"] = "csharp";
        metadata["external"] = false;
        metadata["synthesized"] = true;
        metadata["method_kind"] = target.MethodKind.ToString();
        metadata["signature"] = SymbolKey.Signature(target);
        metadata["parameter_count"] = target.Parameters.Length;
        metadata["is_static"] = target.IsStatic;
        if (target.ContainingType is { } containingType)
        {
            metadata["declaring_type"] = SymbolKey.DisplayType(containingType);
        }

        _output.WriteEntity(new Entity
        {
            Id = id,
            Kind = kind,
            Name = target.Name,
            CanonicalName = SymbolKey.CanonicalName(target),
            Metadata = metadata,
        });

        if (target.ContainingType is { } declaringType)
        {
            var (typeId, typeIsExternal) = ResolveId(declaringType, context);
            if (!typeIsExternal)
            {
                _output.WriteRelation(new Relation
                {
                    Source = typeId,
                    Target = id,
                    Kind = RelationKinds.Declares,
                    Resolved = true,
                    Metadata = Provenance(context),
                });
            }
        }
    }

    /// <summary>Node id for a type, emitting an ExternalType when it is not ours.</summary>
    private string EnsureType(ITypeSymbol type, CallContext context)
    {
        var definition = type.OriginalDefinition;
        var (id, isExternal) = ResolveId(definition, context);

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
            },
        });

        // Tie the type to the assembly node stage 1 created, so a dependency can be
        // traced from a call all the way out to the library it lands in.
        if (assemblyName is not null)
        {
            _output.WriteRelation(new Relation
            {
                Source = EntityId.ExternalSymbol($"A:{assemblyName}", assemblyName),
                Target = id,
                Kind = RelationKinds.Contains,
                Resolved = true,
                Metadata = Provenance(context),
            });
        }

        return id;
    }

    // --- helpers -------------------------------------------------------------

    private string CallerId(ISymbol caller, CallContext context) =>
        SymbolKey.IdFor(caller, context.Compilation, context.ProjectKey, context.TargetFramework);

    private static bool IsReflectionEntryPoint(IMethodSymbol target)
    {
        var containingType = target.ContainingType;
        return containingType is not null
            && ReflectionEntryPoints.Contains($"{containingType.ToDisplayString()}.{target.Name}");
    }

    private SourceSpan Span(SyntaxNode node)
    {
        var lineSpan = node.SyntaxTree.GetLineSpan(node.Span);
        var filePath = PathUtil.ToRepoRelative(_repoRoot, lineSpan.Path);
        return new SourceSpan(
            filePath,
            lineSpan.StartLinePosition.Line + 1,
            lineSpan.StartLinePosition.Character + 1,
            lineSpan.EndLinePosition.Line + 1,
            lineSpan.EndLinePosition.Character + 1);
    }

    private static string Truncate(string text)
    {
        var collapsed = string.Join(' ', text.Split((char[])['\r', '\n', '\t', ' '],
            StringSplitOptions.RemoveEmptyEntries));
        return collapsed.Length <= 120 ? collapsed : collapsed[..120];
    }

    private Dictionary<string, object?> Provenance(CallContext context) => new()
    {
        ["repo_id"] = _options.RepoId,
        ["scan_id"] = _options.ScanId,
        ["resolution_engine"] = "roslyn",
        ["project_key"] = context.ProjectKey,
        ["target_framework"] = context.TargetFramework,
        ["configuration"] = _options.Configuration,
    };
}
