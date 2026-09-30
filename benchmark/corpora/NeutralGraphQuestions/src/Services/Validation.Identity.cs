using Neutral.Domain.Orders;

namespace Neutral.Services;

// Q10: second half of the same partial class, in a different file.
public partial class OrderValidator
{
    public bool HasIdentity(Order order) => order.Id > 0;

    public bool IsValid(Order order)
    {
        // A `this.`-qualified call: the receiver is explicit but it is not a field,
        // so a receiver-text matcher sees "this.HasIdentity".
        return this.HasIdentity(order) && HasPositiveTotal(order);
    }
}
