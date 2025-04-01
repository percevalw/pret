# Metanno

Metanno is a library for building annotation tools. It is built on top of Pret library, and provides a set of components for viewing and editing annotations in tables, texts and images.

Let's use Metanno to view a small list of objects in a table.

```python { .render-with-pret }
from pret.ui.metanno import TableComponent

TableComponent(
    rows=[
        {"name": "Alice", "age": 25, "fries_liker": True, "city": "Paris"},
        {"name": "Bob", "age": 30, "fries_liker": False, "city": "London"},
        {"name": "Charlie", "age": 35, "fries_liker": True, "city": "New York"},
        {"name": "David", "age": 40, "fries_liker": False, "city": "Paris"},
        {"name": "Eve", "age": 45, "fries_liker": True, "city": "London"},
        {"name": "Frank", "age": 50, "fries_liker": False, "city": "New York"},
        {"name": "Grace", "age": 55, "fries_liker": True, "city": "Paris"},
        {"name": "Helen", "age": 60, "fries_liker": False, "city": "London"},
        {"name": "Ivan", "age": 65, "fries_liker": True, "city": "New York"},
    ],
    columns=[
        {"key": "name", "name": "Name", "kind": "text"},
        {"key": "age", "name": "Age", "kind": "number"},
        {"key": "fries_liker", "name": "Likes fries", "kind": "boolean", "editable": True},
        {"key": "city", "name": "City", "kind": "text", "choices": ["Paris", "London", "New York"], "editable": True},
    ],
    row_key="name",
)
```