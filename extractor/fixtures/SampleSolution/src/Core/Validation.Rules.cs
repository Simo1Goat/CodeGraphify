namespace Acme.Core.Orders;

// First half of a partial class. The graph must hold ONE OrderValidator node
// declared in two files, not two nodes.
public partial class OrderValidator
{
    public bool HasPositiveTotal(Order order) => order.Total.Amount > 0;

    // A nested type: containment is by type, not by file or namespace.
    public sealed class Failure
    {
        public Failure(string reason) => Reason = reason;

        public string Reason { get; }

        public enum Severity
        {
            Warning,
            Fatal,
        }
    }
}
