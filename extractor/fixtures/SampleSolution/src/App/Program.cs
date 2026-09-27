using Acme.Core;
using Acme.Core.Data;
using Acme.Core.Orders;

namespace Acme.App;

public static class Program
{
    public static void Main()
    {
        var runner = new OrderRunner(new OrderService());
        runner.RunAll();
        System.Console.WriteLine(PlatformInfo.Name);
    }
}

public class OrderRunner
{
    private readonly IOrderService _service;
    private readonly Repository<OrderEntity> _repo = new();

    // Object creation in a field initializer and in a constructor body:
    // both must produce CREATES edges attributed to the right member.
    public OrderRunner(IOrderService service)
    {
        _service = service;
    }

    public void RunAll()
    {
        var order = new Order(1, new Money(10m, "EUR"));

        // Interface dispatch: compile-time target is IOrderService.Submit(Order).
        _service.Submit(order);

        // Overload resolution: the two-argument overload, same name.
        _service.Submit(order, force: true);

        // Virtual dispatch through a class-typed receiver.
        OrderServiceBase basic = new OrderService();
        basic.Validate(order);

        // Extension method call: resolves through ReducedFrom.
        _repo.AddAll(new[] { new OrderEntity(order.Id) });

        // Generic instance call: normalizes to Repository<T>.Add.
        _repo.Add(new OrderEntity(2));

        // Call from inside a lambda is attributed to RunAll, the containing member.
        var ids = new[] { 1, 2, 3 };
        System.Array.ForEach(ids, id => _service.Cancel(id));

        // Call from inside a local function, likewise.
        LocalHelper();
        void LocalHelper() => _service.Cancel(order.Id);

        // Method group used as a delegate value: RESOLVES_TO, not a call. The
        // method may never run, so recording it as a call would invent control
        // flow that does not exist.
        OrderSubmitted handler = OnOrderSubmitted;
        handler(order);

        DynamicCall(order);
        ReflectionCall();
    }

    private void OnOrderSubmitted(Order order)
    {
        _service.Cancel(order.Id);
    }

    // dynamic: Roslyn cannot name the target. Must become a Dynamic relation
    // carrying source evidence, not a fabricated edge and not a dropped one.
    private void DynamicCall(Order order)
    {
        dynamic loose = _service;
        loose.Submit(order);
    }

    // Reflection: recognized by pattern, recorded as ReflectionBased.
    private void ReflectionCall()
    {
        var type = typeof(OrderService);
        var method = type.GetMethod("Describe");
        method?.Invoke(System.Activator.CreateInstance(type), null);
    }
}

public record OrderEntity(int Id) : IEntity;
