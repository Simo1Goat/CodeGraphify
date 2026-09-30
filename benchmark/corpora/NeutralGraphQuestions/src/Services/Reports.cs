using Neutral.Domain.Reporting;

namespace Neutral.Services;

// Q4: level 1 of the override chain.
public class SummaryReport : ReportBase
{
    public override string Render()
    {
        RaiseRendered("summary");
        return "summary";
    }

    public override string Describe() => "summary report";
}

// Q4: level 2 — overrides the override.
public sealed class DetailedReport : SummaryReport
{
    public override string Render()
    {
        // A base call: this reaches SummaryReport.Render, not DetailedReport.Render,
        // and `base` suppresses virtual dispatch so it is a direct call.
        var head = base.Render();
        return head + " + detail";
    }
}
