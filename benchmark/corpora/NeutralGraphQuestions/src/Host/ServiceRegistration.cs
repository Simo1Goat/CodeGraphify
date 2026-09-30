using Microsoft.Extensions.DependencyInjection;
using Neutral.Domain.Notifications;
using Neutral.Domain.Orders;
using Neutral.Services;

namespace Neutral.Host;

// Q9: DI registrations, composed through an extension method in a separate file
// from the composition root that calls it.
public static class ServiceRegistration
{
    public static IServiceCollection AddNeutralServices(this IServiceCollection services)
    {
        // Three lifetimes.
        services.AddSingleton<INotifier, EmailNotifier>();
        services.AddScoped<IOrderSink, OrderService>();
        services.AddTransient<OrderValidator>();

        // A keyed registration.
        services.AddKeyedScoped<IOrderSink, AuditingOrderService>("audit");

        return services;
    }
}
