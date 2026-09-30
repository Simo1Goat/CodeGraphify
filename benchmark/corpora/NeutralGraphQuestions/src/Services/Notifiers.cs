using Neutral.Domain.Notifications;

namespace Neutral.Services;

public sealed class EmailNotifier : INotifier
{
    private readonly NotificationLog _log = new("email");

    public void Notify(string message)
    {
        // Q1: call site 2 of 3 for the log.
        _log.Add(message);
    }

    public int Delivered => _log.Count;
}

public sealed class SmsNotifier : INotifier
{
    public void Notify(string message)
    {
    }
}
