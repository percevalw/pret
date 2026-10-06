"""
Preserve source locations through marshaling for the offline coverage converter
"""

import ast
import hashlib
import inspect
import json
import os
import textwrap
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=None)
def read_source(filename):
    """
    Read a source file once per process for bundle snapshots and literal locations
    """
    path = Path(filename).resolve()
    source = path.read_text()
    return {
        "path": str(path),
        "source": source,
        "sha256": hashlib.sha256(source.encode()).hexdigest(),
    }


@lru_cache(maxsize=None)
def js_literals(filename):
    """
    Locate literal marshal overrides and selected module constants for coverage inventory
    """
    snapshot = read_source(filename)
    tree = ast.parse(snapshot["source"])
    constants = {
        target.id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    literals = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "marshal_as":
            continue
        for keyword in node.keywords:
            if keyword.arg != "js":
                continue
            candidates = [keyword.value]
            if isinstance(keyword.value, ast.IfExp):
                candidates = [keyword.value.body, keyword.value.orelse]
            for candidate in candidates:
                if isinstance(candidate, ast.Name):
                    candidate = constants.get(candidate.id)
                if not isinstance(candidate, ast.Constant) or not isinstance(candidate.value, str):
                    continue
                code = candidate.value
                # Escaped newlines and joined literals need a generated source entry
                segment = ast.get_source_segment(snapshot["source"], candidate)
                if len(code.split("\n")) != len(segment.split("\n")):
                    continue
                literals[candidate.lineno] = {
                    "code": code,
                    "path": snapshot["path"],
                    "lines": list(range(candidate.lineno, candidate.lineno + len(code.split("\n")))),
                }
    return list(literals.values())


def js_origin(code, filename, lineno):
    """
    Associate an override with its literal or record the dynamic string call site
    """
    if Path(filename).is_file():
        matches = [literal for literal in js_literals(filename) if literal["code"] == code]
        if matches:
            return min(matches, key=lambda literal: abs(literal["lines"][0] - lineno))
    return {"code": code, "path": str(Path(filename).resolve()), "call_line": lineno}


def function_source(value, factory_name, scoped_vars):
    """
    Wrap inspected Python without changing its body lines for Transcrypt source mapping
    """
    lines, first_line = inspect.getsourcelines(value)
    code = textwrap.dedent("".join(lines))
    function = ast.parse(code).body[0]
    body = code.splitlines(keepends=True)
    for idx in range(function.lineno - 1):
        body[idx] = "\n"
    filename = str(Path(inspect.getsourcefile(value)).resolve())
    code = f"def {factory_name}({', '.join(scoped_vars)}):\n" + textwrap.indent("".join(body), "  ")
    if not code.endswith("\n"):
        code += "\n"
    code += f"  return {value.__name__}\n"
    origins = (
        [None] + [[filename, first_line + idx] if line.strip() else None for idx, line in enumerate(body)] + [None]
    )
    return code, origins


def save_bundle(directory, modules, order, code, source_codes, origins, excluded):
    """
    Save Transcrypt maps and bundle layout for the Node coverage conversion stage
    """
    output = Path(os.environ["PRET_COVERAGE_DIR"]) / "bundles"
    output.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(code.encode()).hexdigest()
    url = f"pret-coverage://{digest}.js"
    code += f"\n//# sourceURL={url}"
    python_lines = {}
    offset = 1
    for name, (language, text) in source_codes.items():
        if language == "py":
            for idx, origin in enumerate(origins.get(name, [])):
                if origin is not None:
                    python_lines[offset + idx + 1] = origin
            offset += text.count("\n") + 1

    main_js_lines = {}
    # Transcrypt writes the main map before PRET appends the JavaScript overrides
    main = (directory / "__target__" / "__main__.js").read_text()
    appended = "\n".join(text for language, text in source_codes.values() if language == "js")
    offset = main[: len(main) - len(appended)].count("\n") if appended else 0
    generated = {}
    for name, (language, text) in source_codes.items():
        if language != "js":
            continue
        origin = origins[name]
        stripped = origin["code"].strip()
        leading = origin["code"][: len(origin["code"]) - len(origin["code"].lstrip())].count("\n")
        if "lines" in origin:
            for idx, line in enumerate(origin["lines"][leading : leading + len(stripped.splitlines())]):
                main_js_lines[offset + idx + 2] = [origin["path"], line]
        else:
            identity = f"{origin['path']}:{origin['call_line']}:{stripped}"
            filename = str(output / f"{hashlib.sha256(identity.encode()).hexdigest()}.js")
            Path(filename).write_text(stripped + "\n")
            generated[filename] = {
                **read_source(filename),
                "origin": [origin["path"], origin["call_line"]],
            }
            for idx in range(len(stripped.splitlines())):
                main_js_lines[offset + idx + 2] = [filename, idx + 1]
        offset += text.count("\n") + 1

    layout = []
    offset = 3
    for name in order:
        if name == "__main__":
            layout.append(
                {
                    "line": offset,
                    "map": json.loads((directory / "__target__" / "__main__.map").read_text()),
                    "python": python_lines,
                    "javascript": main_js_lines,
                }
            )
        offset += modules[name].count("\n") + 2
    filenames = {origin[0] for origin in [*python_lines.values(), *main_js_lines.values()]}
    (output / f"{digest}.json").write_text(
        json.dumps(
            {
                "url": url,
                "code": code,
                "modules": layout,
                "sources": {filename: generated.get(filename) or read_source(filename) for filename in filenames},
                "excluded": excluded,
            }
        )
    )
    return code
