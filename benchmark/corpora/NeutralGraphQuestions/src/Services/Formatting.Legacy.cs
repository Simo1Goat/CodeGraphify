namespace Neutral.Services.Legacy;

// Same simple name as Neutral.Services.Text.Formatter, different namespace and
// different behaviour.
public static class Formatter
{
    public static string Render(string value) => value.ToUpperInvariant();
}
