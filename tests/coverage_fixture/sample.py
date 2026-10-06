"""
Python and embedded JavaScript sources whose lines the coverage integration test maps back
"""

import json
import linecache
import sys
from pathlib import Path

from pret.marshal import PretMarshaler, marshal_as


def decorated(function):
    return function


@decorated
def choose(value):
    """
    Return distinct values through a nested function and a multiline expression
    """
    if value:
        # Keep physical expression lines distinct for the source mapping check
        result = 10 + 1

        def nested():
            return result

        return nested()
    return -11


@marshal_as(
    js="""
return function chooseJS(value) {
    if (value) {
        return 21;
    }
    return -21;
}
"""
)
def choose_js(value):
    """
    Keep the Python replacement untouched when the JavaScript override executes
    """
    return -999


@marshal_as(js=f"return function dynamic() {{ return {31}; }}")
def dynamic():
    return -31


def untouched():
    return 101


if __name__ == "__main__":
    assert choose(False) == -11
    # Notebook definitions have inspectable cached source without a file on disk
    cell = "def notebook_function():\n    return 43\n"
    linecache.cache["<coverage-cell>"] = (
        len(cell),
        None,
        cell.splitlines(keepends=True),
        "<coverage-cell>",
    )
    namespace = {}
    exec(compile(cell, "<coverage-cell>", "exec"), namespace)
    (_, code), _ = PretMarshaler().dump(
        {
            "choose": choose,
            "choose_js": choose_js,
            "dynamic": dynamic,
            "notebook_function": namespace["notebook_function"],
            "lambda": lambda: 41,
        }
    )
    Path(sys.argv[1]).write_text(json.dumps({"code": code}))
