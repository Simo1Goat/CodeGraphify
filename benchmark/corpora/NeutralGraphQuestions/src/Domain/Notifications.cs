namespace Neutral.Domain.Notifications;

public interface INotifier
{
    void Notify(string message);
}

// Q11: a class with a field, a constructor and a property, so the inventory of
// member kinds is not all methods.
public sealed class NotificationLog
{
    private readonly List<string> _entries = new();

    public NotificationLog(string channel)
    {
        Channel = channel;
    }

    public string Channel { get; }

    public int Count => _entries.Count;

    public void Add(string entry)
    {
        _entries.Add(entry);
    }
}
