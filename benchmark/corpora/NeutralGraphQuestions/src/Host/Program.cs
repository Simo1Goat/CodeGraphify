using System.Text.Json;
using Microsoft.Extensions.DependencyInjection;
using Neutral.Domain.Notifications;
using Neutral.Domain.Orders;
using Neutral.Services;
using Neutral.Services.Data;
using Neutral.Services.Text;

namespace Neutral.Host;

public static class Program
{
    public static void Main()
    {
        var services = new ServiceCollection();

        // Q9: cross-file extension-method composition of the container.
        services.AddNeutralServices();

        var runner = new OrderRunner(new OrderService(), new EmailNotifier());
        runner.Run();
    }
}

public sealed class OrderRunner
{
    private readonly IOrderSink _sink;
    private readonly INotifier _notifier;

    // Q6: instantiation in a FIELD INITIALIZER.
    private readonly Repository<OrderEntity> _repository = new();

    // Q9: constructor injection — the parameters a DI registration must satisfy.
    public OrderRunner(IOrderSink sink, INotifier notifier)
    {
        _sink = sink;
        _notifier = notifier;
    }

    public void Run()
    {
        // Q6: instantiation inside a METHOD.
        var order = new Order(1, new Money(10m, "EUR"));

        // Q5: the ONE-argument overload.
        _sink.Accept(order);

        // Q5: the TWO-argument overload — same name, same receiver, different target.
        _sink.Accept(order, force: true);

        // Q1: call site 3 of 3 for PriceCalculator.Apply.
        var raised = PriceCalculator.Apply(order.Total, 1.2m);

        // Q5: the type-differing overload set — these two reach DIFFERENT methods.
        var byInt = PriceFormatter.Format(3);
        var byMoney = PriceFormatter.Format(raised);

        // A BARE, receiver-less call. A receiver-text matcher drops this entirely.
        Announce(byInt + byMoney);

        // Q2: an extension method, written as an instance call.
        _repository.AddAll(new[] { new OrderEntity(order.Id) });

        // Q2: a generic instance call that must normalise to Repository<T>.Add.
        _repository.Add(new OrderEntity(2));

        // Q8: a call into a third-party framework API.
        var json = JsonSerializer.Serialize(order);

        // Two types named Formatter exist, in Neutral.Services.Text and
        // Neutral.Services.Legacy. The `using` above decides which one this is;
        // an analyzer that indexes by bare name repo-wide cannot tell them apart.
        _notifier.Notify(Formatter.Render(json));

        // Q6: instantiation through a factory method.
        var report = ReportFactory.Create();
        report.Render();

        DynamicCall(order);
        ReflectionCall();
    }

    private void Announce(string message)
    {
        _notifier.Notify(message);
    }

    // Q12: `dynamic` — the target is chosen at run time and cannot be named.
    private void DynamicCall(Order order)
    {
        dynamic loose = _sink;
        loose.Accept(order);
    }

    // Q12: reflection — an unknown method is invoked here.
    private void ReflectionCall()
    {
        var type = typeof(OrderService);
        var method = type.GetMethod("AcceptedCount");
        method?.Invoke(Activator.CreateInstance(type), null);
    }
}

public static class ReportFactory
{
    public static SummaryReport Create() => new DetailedReport();
}

public record OrderEntity(int Id) : IEntity;
