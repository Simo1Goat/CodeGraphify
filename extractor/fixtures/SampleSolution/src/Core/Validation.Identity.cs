namespace Acme.Core.Orders;

// Second half of the same partial class, in a different file.
public partial class OrderValidator
{
    public bool HasIdentity(Order order) => order.Id > 0;

    public Failure? Validate(Order order) =>
        HasIdentity(order) && HasPositiveTotal(order) ? null : new Failure("invalid");
}
