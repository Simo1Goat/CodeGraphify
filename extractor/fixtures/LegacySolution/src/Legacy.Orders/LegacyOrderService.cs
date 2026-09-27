using System.Collections.Generic;

namespace Legacy.Orders
{
    public class LegacyOrderService
    {
        private readonly List<int> _submitted = new List<int>();

        public void Submit(int orderId)
        {
            _submitted.Add(orderId);
        }

        public int Count()
        {
            return _submitted.Count;
        }
    }
}
