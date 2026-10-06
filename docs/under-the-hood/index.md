# Under the hood

These pages explain how Pret works inside, for readers who want to understand what happens when they use it, or who
build their own components and data sources on top of it. They assume you have followed the
[tutorials](../tutorials/index.md).

=== card {: href=/under-the-hood/python-in-the-browser/ }

    :fontawesome-brands-python:
    **How Python components run in the browser**

    ---
    How your functions are translated to JavaScript, how the values they use travel with them, and how the
    browser calls back into Python.

=== card {: href=/under-the-hood/store-files/ }

    :fontawesome-solid-database:
    **Inside a Store file**

    ---
    What a `.pret` file contains, why it is a SQLite database, and where each kind of Store value is saved.

=== card {: href=/under-the-hood/store-edits/ }

    :fontawesome-solid-pen-to-square:
    **What happens when you edit a Store**

    ---
    How an edit travels from the page to Python and to other pages, and what happens on conflicts, lost replies
    and undo.

=== card {: href=/under-the-hood/dataframe-queries/ }

    :fontawesome-solid-table-list:
    **How widgets query DataFrames**

    ---
    How widgets read large tables page by page, preview Spark and Ibis queries, and connect to other sources.
