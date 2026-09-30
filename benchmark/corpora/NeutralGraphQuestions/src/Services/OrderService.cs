using Neutral.Domain.Orders;

namespace Neutral.Services;

// Q3: implementation 1 of 3 — implicit implementation.
public class OrderService : IOrderSink
{
    private readonly List<Order> _accepted = new();

    // Q5: implements the arity-differing overload set.
    public int Accept(Order order)
    {
        // Q2 + Q1: an in-repo call across a project boundary.
        var discounted = PriceCalculator.Apply(order.Total, 0.9m);
        _accepted.Add(order with { Total = discounted });
        return order.Id;
    }

    public int Accept(Order order, bool force) => force ? Accept(order) : -1;

    public void Discard(int orderId) => _accepted.RemoveAll(o => o.Id == orderId);

    public int AcceptedCount() => _accepted.Count;
}

// Q3: implementation 2 of 3.
public class AuditingOrderService : IOrderSink
{
    public int Accept(Order order) => order.Id;

    public int Accept(Order order, bool force) => order.Id;

    public void Discard(int orderId)
    {
    }
}

// Q3: implementation 3 of 3 — EXPLICIT. Its members are not named `Accept`, so
// a name-matching analyzer cannot find them.
public class ExplicitOrderService : IOrderSink
{
    int IOrderSink.Accept(Order order) => 0;

    int IOrderSink.Accept(Order order, bool force) => 0;

    void IOrderSink.Discard(int orderId)
    {
    }
}
