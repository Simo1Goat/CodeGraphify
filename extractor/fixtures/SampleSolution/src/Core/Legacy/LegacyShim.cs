namespace Acme.Core.Legacy
{
    // Compiled only into the netstandard2.0 variant (see Core.csproj), so the
    // two variants of this project do not declare the same set of types.
    public static class LegacyShim
    {
        public static string Describe() => "legacy";
    }
}

namespace System.Runtime.CompilerServices
{
    // Polyfill so `record` compiles on netstandard2.0. A real multi-targeting
    // codebase needs this.
    internal static class IsExternalInit { }
}
