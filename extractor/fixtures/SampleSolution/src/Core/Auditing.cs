using System;

namespace Acme.Core.Orders;

public interface IAuditable
{
    string AuditName { get; }
}

// An interface extending other interfaces: INHERITS, not IMPLEMENTS -- an
// interface does not implement anything.
public interface IAuditableOrderService : IOrderService, IAuditable
{
}

// Inherits an EXTERNAL base class. Unlike the implicit object base, this is a real
// dependency on a framework type and must appear in the graph.
public class OrderException : InvalidOperationException
{
    public OrderException(string message) : base(message)
    {
    }
}

// Implements one of our interfaces and one external interface, and inherits an
// internal base whose members already implement IOrderService -- so
// IMPLEMENTS_MEMBER must not be re-emitted here for those inherited members.
public class TrackedOrderService : OrderService, IAuditable, IDisposable
{
    public string AuditName => "tracked";

    public void Dispose()
    {
    }
}
