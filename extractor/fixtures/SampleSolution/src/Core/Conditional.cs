namespace Acme.Core;

public static class PlatformInfo
{
    public static string Name =>
#if NET8_0_OR_GREATER
        ModernOnly();
#else
        LegacyOnly();
#endif

#if NET8_0_OR_GREATER
    // Present only in the net8.0 variant.
    private static string ModernOnly() => "net8";
#else
    // Present only in the netstandard2.0 variant.
    private static string LegacyOnly() => "netstandard";
#endif
}
