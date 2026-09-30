using Neutral.Domain.Orders;

namespace Neutral.Services;

// Q10: first half of a partial class. The graph should hold ONE OrderValidator
// declared in two files, not two separate types.
public partial class OrderValidator
{
    public bool HasPositiveTotal(Order order) => order.Total.Amount > 0;
}
