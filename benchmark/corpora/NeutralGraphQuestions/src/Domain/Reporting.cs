namespace Neutral.Domain.Reporting;

// Q4: an abstract member plus a virtual member, overridden across two levels
// (ReportBase -> SummaryReport -> DetailedReport, in the Services project).
public abstract class ReportBase
{
    // Q11: a protected field and an event.
    protected int RenderCount;

    public event Action<string>? Rendered;

    public abstract string Render();

    public virtual string Describe() => "report";

    protected void RaiseRendered(string payload)
    {
        // Q12 companion: an ordinary null-conditional event invocation.
        Rendered?.Invoke(payload);
        RenderCount++;
    }
}
