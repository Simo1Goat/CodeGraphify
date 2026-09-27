using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace CodeGraphify.Extractor.Calls;

/// <summary>
/// Finds which declared member a call site belongs to.
///
/// Not simply the nearest enclosing declaration: lambdas and local functions are
/// walked straight through, so a call inside <c>items.ForEach(x =&gt; Send(x))</c>
/// is attributed to the method containing the lambda. Neither is a node in phase 1,
/// and attributing to them would leave the call dangling off something the graph
/// does not contain.
///
/// Accessors resolve to their property or event for the same reason: stage 2 emits
/// the Property node rather than get_X and set_X, so a call from inside a getter
/// belongs to the property.
/// </summary>
public static class CallerResolver
{
    public static ISymbol? Find(SyntaxNode node, SemanticModel model)
    {
        for (var current = node; current is not null; current = current.Parent)
        {
            switch (current)
            {
                // Walked through: not nodes in phase 1, so calls inside them belong
                // to the enclosing member.
                case LambdaExpressionSyntax:
                case AnonymousMethodExpressionSyntax:
                case LocalFunctionStatementSyntax:
                    continue;

                case AccessorDeclarationSyntax accessor:
                {
                    var accessorSymbol = model.GetDeclaredSymbol(accessor) as IMethodSymbol;
                    // AssociatedSymbol is the property or event the accessor serves.
                    return accessorSymbol?.AssociatedSymbol ?? accessorSymbol;
                }

                case MethodDeclarationSyntax method:
                    return model.GetDeclaredSymbol(method);

                case ConstructorDeclarationSyntax constructor:
                    return model.GetDeclaredSymbol(constructor);

                case DestructorDeclarationSyntax destructor:
                    return model.GetDeclaredSymbol(destructor);

                case OperatorDeclarationSyntax op:
                    return model.GetDeclaredSymbol(op);

                case ConversionOperatorDeclarationSyntax conversion:
                    return model.GetDeclaredSymbol(conversion);

                // An expression-bodied property or an initializer: the property is
                // the caller.
                case PropertyDeclarationSyntax property:
                    return model.GetDeclaredSymbol(property);

                case IndexerDeclarationSyntax indexer:
                    return model.GetDeclaredSymbol(indexer);

                case EventDeclarationSyntax eventDeclaration:
                    return model.GetDeclaredSymbol(eventDeclaration);

                // A field or event-field initializer: the field is the caller, which
                // is what makes `private readonly X _x = new();` attributable.
                case VariableDeclaratorSyntax declarator
                    when declarator.Parent?.Parent is BaseFieldDeclarationSyntax:
                    return model.GetDeclaredSymbol(declarator);

                // A record's primary constructor parameter list, or a base type
                // argument list: attribute to the type itself.
                case RecordDeclarationSyntax record:
                    return model.GetDeclaredSymbol(record);

                case ClassDeclarationSyntax classDeclaration:
                    return model.GetDeclaredSymbol(classDeclaration);

                case StructDeclarationSyntax structDeclaration:
                    return model.GetDeclaredSymbol(structDeclaration);
            }
        }

        return null;
    }
}
