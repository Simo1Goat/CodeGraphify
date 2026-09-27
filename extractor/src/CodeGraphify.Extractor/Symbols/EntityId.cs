using System.Security.Cryptography;
using System.Text;

namespace CodeGraphify.Extractor.Symbols;

/// <summary>
/// Node identity. This is the C# mirror of app/contract/identity.py; the policy
/// and its reasoning are documented there, and tests/test_identity_parity.py
/// asserts the two produce the same ids.
/// </summary>
public static class EntityId
{
    /// <summary>Bump when the hashing scheme changes; forces a full re-scan.</summary>
    public const string IdentityVersion = "v1";

    private const int DigestChars = 32;

    private static string Hash(string prefix, params string?[] parts)
    {
        var sb = new StringBuilder();
        sb.Append(IdentityVersion).Append('|').Append(prefix);
        foreach (var part in parts)
        {
            sb.Append('|').Append(part ?? string.Empty);
        }

        var digest = SHA256.HashData(Encoding.UTF8.GetBytes(sb.ToString()));
        return string.Concat(prefix, ":", Convert.ToHexString(digest).ToLowerInvariant()[..DigestChars]);
    }

    public static string Repository(string repoId) => Hash("repo", repoId);

    public static string Solution(string solutionKey) => Hash("sln", solutionKey);

    public static string Project(string projectKey) => Hash("proj", projectKey);

    public static string Variant(string projectKey, string? targetFramework, string configuration) =>
        Hash("var", projectKey, targetFramework, configuration);

    public static string File(string repoRelativePath) => Hash("file", repoRelativePath);

    public static string Namespace(string namespaceName) => Hash("ns", namespaceName);

    /// <summary>A type or member declared in an analyzed project.</summary>
    public static string Symbol(string canonicalName, string projectKey, string? targetFramework) =>
        Hash("sym", canonicalName, projectKey, targetFramework);

    /// <summary>A type or member from a referenced assembly, shared across projects.</summary>
    public static string ExternalSymbol(string canonicalName, string? assemblyName) =>
        Hash("ext", canonicalName, assemblyName);

    public static string Unresolved(string filePath, int startLine, int startColumn, string expressionText) =>
        Hash("unres", filePath, startLine.ToString(), startColumn.ToString(), expressionText);
}
