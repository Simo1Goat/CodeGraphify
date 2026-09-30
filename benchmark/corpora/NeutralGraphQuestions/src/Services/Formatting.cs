namespace Neutral.Services.Text;

// Two different types with the SAME simple name in different namespaces. An
// analyzer that indexes by bare name repo-wide must collapse these onto one.
public static class Formatter
{
    public static string Render(string value) => value.Trim();
}
