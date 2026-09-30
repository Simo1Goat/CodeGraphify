namespace Neutral.Domain.Orders;

// Q11: an enum, a struct, a record — the declaration kinds a graph must inventory.
public enum OrderState
{
    Draft,
    Placed,
    Cancelled,
}

public readonly struct Money
{
    public Money(decimal amount, string currency)
    {
        Amount = amount;
        Currency = currency;
    }

    public decimal Amount { get; }

    public string Currency { get; }
}

public record Order(int Id, Money Total)
{
    public OrderState State { get; set; } = OrderState.Draft;
}

// Q11: a delegate declaration.
public delegate void OrderPlaced(Order order);

// Q3: one interface, three implementations (one of them explicit).
public interface IOrderSink
{
    // Q5: an overload set that differs by ARITY.
    int Accept(Order order);

    int Accept(Order order, bool force);

    void Discard(int orderId);
}

// Q5: a second overload set that differs by PARAMETER TYPE, not arity.
public static class PriceFormatter
{
    public static string Format(int amount) => $"{amount} units";

    public static string Format(Money money) => $"{money.Amount} {money.Currency}";
}

// Q1: the method called from three sites across two projects.
public static class PriceCalculator
{
    public static Money Apply(Money total, decimal rate) =>
        new Money(total.Amount * rate, total.Currency);
}
