using System.Collections.Generic;
using System.Linq;

namespace Acme.Core.Data;

public interface IEntity { int Id { get; } }

// Generic type: calls to Repository<Order>.Add must normalize to the
// OriginalDefinition Repository<T>.Add, not a per-instantiation node.
public class Repository<T> where T : IEntity
{
    private readonly List<T> _items = new();

    public void Add(T item) => _items.Add(item);

    public T? Find(int id) => _items.FirstOrDefault(x => x.Id == id);

    public int Count() => _items.Count;
}

public static class RepositoryExtensions
{
    // Extension method: a reduced call site (repo.AddAll(...)) must resolve
    // through ReducedFrom back to this declaration.
    public static void AddAll<T>(this Repository<T> repo, IEnumerable<T> items)
        where T : IEntity
    {
        foreach (var item in items) repo.Add(item);
    }
}
