using System;
using System.Collections.Generic;

namespace Acme.Core.Orders;

public enum OrderState { Draft, Submitted, Cancelled }

public readonly struct Money
{
    public Money(decimal amount, string currency) { Amount = amount; Currency = currency; }
    public decimal Amount { get; }
    public string Currency { get; }
}

public record Order(int Id, Money Total)
{
    public OrderState State { get; set; } = OrderState.Draft;
}

public delegate void OrderSubmitted(Order order);

public interface IOrderService
{
    // Overloads: call resolution must pick the right one, not just the name.
    int Submit(Order order);
    int Submit(Order order, bool force);
    void Cancel(int id);
}

public abstract class OrderServiceBase
{
    // Virtual with an override in the derived class -> OVERRIDES edge.
    public virtual int Validate(Order order) => order.Id > 0 ? 0 : 1;

    // Abstract: implemented, never called directly.
    public abstract string Describe();

    // Virtual property, overridden below: OVERRIDES applies to properties too, not
    // only to methods.
    public virtual string Kind => "base";
}

public class OrderService : OrderServiceBase, IOrderService
{
    private readonly List<Order> _orders = new();
    public event OrderSubmitted? Submitted;

    public int Submit(Order order)
    {
        // Direct call to a base implementation via base. -> CALLS_DIRECT,
        // never CALLS_VIRTUAL, because base. suppresses virtual dispatch.
        var problems = base.Validate(order);
        if (problems != 0) return -1;

        _orders.Add(order);
        order.State = OrderState.Submitted;
        Submitted?.Invoke(order);
        return order.Id;
    }

    // Overload that calls the other overload on the same type.
    public int Submit(Order order, bool force) => force ? Submit(order) : -1;

    public void Cancel(int id) => _orders.RemoveAll(o => o.Id == id);

    // Overrides the virtual base member.
    public override int Validate(Order order) => order.Total.Amount > 0 ? 0 : 2;

    public override string Describe() => nameof(OrderService);

    public override string Kind => "orders";
}

public class MockOrderService : IOrderService
{
    // Second implementation, so MAY_DISPATCH_TO has more than one target
    // to find in phase 2.
    public int Submit(Order order) => order.Id;
    public int Submit(Order order, bool force) => order.Id;
    public void Cancel(int id) { }
}

public class ExplicitOrderService : IOrderService
{
    // Explicit interface implementation: FindImplementationForInterfaceMember
    // must find these, a name match would not.
    int IOrderService.Submit(Order order) => 0;
    int IOrderService.Submit(Order order, bool force) => 0;
    void IOrderService.Cancel(int id) { }
}
