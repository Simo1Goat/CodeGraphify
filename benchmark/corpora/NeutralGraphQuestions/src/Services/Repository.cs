namespace Neutral.Services.Data;

public interface IEntity
{
    int Id { get; }
}

// Q2: a generic type whose members must normalise to their definition.
public class Repository<T> where T : IEntity
{
    private readonly List<T> _items = new();

    public void Add(T item) => _items.Add(item);

    public int Count() => _items.Count;
}

public static class RepositoryExtensions
{
    // An extension method: written as an instance call, declared as a static
    // method taking the receiver as its first parameter.
    public static void AddAll<T>(this Repository<T> repository, IEnumerable<T> items)
        where T : IEntity
    {
        foreach (var item in items)
        {
            repository.Add(item);
        }
    }
}
