using System.Text.RegularExpressions;

namespace Acme.App;

// Exercises source generators without needing Razor: [GeneratedRegex] makes the
// SDK's regex generator emit the body of this partial method. MSBuildWorkspace
// loads generators but never runs them, so without the extractor running them
// itself the generated half does not exist -- and on a real ASP.NET solution that
// was 60% of the call graph.
public static partial class TextRules
{
    [GeneratedRegex(@"^ORD-\d+$", RegexOptions.CultureInvariant)]
    public static partial Regex OrderCode();

    public static bool IsOrderCode(string value) => OrderCode().IsMatch(value);
}
