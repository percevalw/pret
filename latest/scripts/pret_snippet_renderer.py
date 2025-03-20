import ast
import os
import shutil
import tempfile
from pathlib import Path
from textwrap import dedent
from typing import Tuple

import astunparse
from markdown.extensions import Extension
from markdown.extensions.attr_list import get_attrs
from markdown.extensions.codehilite import parse_hl_lines
from markdown.extensions.fenced_code import FencedBlockPreprocessor
from mkdocs.config.config_options import Type as MkType
from mkdocs.config.defaults import MkDocsConfig
from mkdocs.plugins import BasePlugin
from mkdocstrings.extension import AutoDocProcessor
from mkdocstrings.plugin import MkdocstringsPlugin
from regex import regex

from pret.main import build
from pret.serialize import clear_shared_pickler

BRACKET_RE = regex.compile(r"\[([^\[]+)\]")
CITE_RE = regex.compile(r"@([\w_:-]+)")
DEF_RE = regex.compile(r"\A {0,3}\[@([\w_:-]+)\]:\s*(.*)")
INDENT_RE = regex.compile(r"\A\t| {4}(.*)")

CITATION_RE = r"(\[@(?:[\w_:-]+)(?: *, *@(?:[\w_:-]+))*\])"


class PyCodePreprocessor(FencedBlockPreprocessor):
    """Gather reference definitions and citation keys"""

    FENCED_BLOCK_RE = regex.compile(
        dedent(
            r"""
            (?P<fence>^[ ]*(?:~{3,}|`{3,}))[ ]*                          # opening fence
            ((\{(?P<attrs>[^\}\n]*)\})|                              # (optional {attrs} or
            (\.?(?P<lang>[\w#.+-]*)[ ]*(\{(?P<attrs>[^\n]*)\})?)?    # optional (.)lang
            (hl_lines=(?P<quot>"|')(?P<hl_lines>.*?)(?P=quot)[ ]*)?) # optional hl_lines)
            \n                                                       # newline (end of opening fence)
            (?P<code>.*?)(?<=\n)                                     # the code block
            (?P=fence)[ ]*$                                          # closing fence
        """  # noqa: E501
        ),
        regex.MULTILINE | regex.DOTALL | regex.VERBOSE,
    )

    def __init__(self, md, code_blocks):
        super().__init__(md, {})
        self.code_blocks = code_blocks

    def run(self, lines):
        new_text = ""
        text = "\n".join(lines)
        num_pret_code_blocks = 0
        while True:
            # ----  https://github.com/Python-Markdown/markdown/blob/5a2fee/markdown/extensions/fenced_code.py#L84C9-L98  # noqa: E501
            m = self.FENCED_BLOCK_RE.search(text)
            if m:
                lang, id, classes, config = None, "", [], {}
                if m.group("attrs"):
                    id, classes, config = self.handle_attrs(get_attrs(m.group("attrs")))
                    if m.group("lang"):
                        lang = m.group("lang")
                    elif len(classes):
                        lang = classes.pop(0)
                else:
                    if m.group("lang"):
                        lang = m.group("lang")
                    if m.group("hl_lines"):
                        # Support `hl_lines` outside of `attrs` for
                        # backward-compatibility
                        config["hl_lines"] = parse_hl_lines(m.group("hl_lines"))
                # ----
                code = m.group("code")

                if lang == "python" and "no-exec" not in classes:
                    self.code_blocks.append(
                        {
                            "code": dedent(code),
                            "render": "render-with-pret" in classes,
                            "id": num_pret_code_blocks,
                        }
                    )
                    if "render-with-pret" in classes:
                        new_text += text[: m.start()]
                        new_text += (
                            '<div class="pret-code-snippet" >\n'
                            + text[m.start() : m.end()]
                            + f'\n<div class="pret-code-snippet-view-container">'
                            f'<div class="pret-code-snippet-view-content" data-pret-chunk-idx="{num_pret_code_blocks}" />'  # noqa: E501
                            f"</div>"
                            f"</div>\n"
                        )
                        num_pret_code_blocks += 1
                        text = text[m.end() :]
                        continue
                new_text += text[: m.end()]
                text = text[m.end() :]
            else:
                break

        new_text += text[:]

        return new_text.strip().split("\n")


class PyCodeExtension(Extension):
    def __init__(self, code_blocks):
        super(PyCodeExtension, self).__init__()
        self.code_blocks = code_blocks

    def extendMarkdown(self, md):
        self.md = md
        md.registerExtension(self)
        md.preprocessors.register(
            PyCodePreprocessor(md, self.code_blocks), "fenced_code", 31
        )
        for ext in md.registeredExtensions:
            if isinstance(ext, AutoDocProcessor):
                ext._config["mdx"].append(self)


def run_code_with_result(code, env, tmp_dir, filename: str, block_idx):
    # Parse the code into an AST
    tree = ast.parse(code, mode="exec")
    # Check if the last statement is an expression
    *body, last_expr = tree.body
    # Execute all statements except the last expression
    new_body = ast.Module(
        body=[
            *body,
            # assign last_expr to "ret_value"
            ast.Assign(
                targets=[ast.Name(id=f"ret_value_{block_idx}", ctx=ast.Store())],
                value=last_expr.value,
            )
            if isinstance(last_expr, ast.Expr)
            else last_expr,
        ]
    )
    new_body = ast.fix_missing_locations(new_body)
    new_body = astunparse.unparse(new_body)
    tmp_filename = Path(tmp_dir) / filename
    tmp_filename.write_text(new_body)
    exec(compile(new_body, tmp_filename, "exec"), env)
    # run tmp_file
    ret_value = env.get(f"ret_value_{block_idx}")
    return ret_value


class PretSnippetRendererPlugin(BasePlugin):
    config_scheme: Tuple[Tuple[str, MkType]] = (
        # ("bibtex_file", MkType(str)),  # type: ignore[assignment]
        # ("order", MkType(str, default="unsorted")),  # type: ignore[assignment]
    )

    def __init__(self):
        self.page_code_blocks = []
        self.docs_code_blocks = {}
        self.assets = {}
        self.entries = set()

    def on_config(self, config: MkDocsConfig):
        self.ext = PyCodeExtension(self.page_code_blocks)
        # After pymdownx.highlight, because of weird registering deleting the first
        # extension
        config["markdown_extensions"].append(self.ext)
        config["markdown_extensions"].remove("pymdownx.highlight")
        config["markdown_extensions"].remove("fenced_code")

    def on_pre_build(self, *, config: MkDocsConfig):
        mkdocstrings_plugin: MkdocstringsPlugin = config.plugins["mkdocstrings"]
        mkdocstrings_plugin.get_handler("python")

    def on_page_content(self, html, page, config, files):
        if len(self.page_code_blocks):
            self.docs_code_blocks[str(page.url)] = list(self.page_code_blocks)
        self.page_code_blocks.clear()

        page_code_blocks = self.docs_code_blocks.get(str(page.url))
        url_depth_count = page.url.count("/")
        assets_dir = "../" * url_depth_count + "assets/"
        if page_code_blocks:
            with tempfile.TemporaryDirectory() as tmp_dir:
                renderables = []
                env = {}
                for block_idx, code_block in enumerate(page_code_blocks):
                    filename = f"{page.url}_{block_idx}.py".strip("/").replace(
                        "/", "__"
                    )
                    result = run_code_with_result(
                        code_block["code"],
                        env,
                        tmp_dir,
                        filename,
                        block_idx,
                    )
                    if code_block["render"]:
                        renderables.append(result)
                page_code_blocks.clear()

                with build(renderables, mode="federated") as (
                    assets,
                    entries,
                    pickle_filename,
                ):
                    remote_imports = str([n for _, n in entries if n is not None])
                    html = (
                        "<script>"
                        f"window.PRET_PICKLE_FILE = '{assets_dir + pickle_filename}';"
                        f"window.PRET_REMOTE_IMPORTS = {remote_imports};"
                        "</script>" + html
                    )
                    self.assets.update(assets)
                    self.entries.update(entries)

        clear_shared_pickler()

        return html

    def on_post_page(self, output, page, config):
        url_depth_count = page.url.count("/")
        assets_dir = "../" * url_depth_count + "assets/"
        webpack_trigger = '<script defer src="'
        webpack_bundle = self.assets["index.html"].split(webpack_trigger)[1]
        webpack_bundle = webpack_bundle.split('">')[0]
        output = output.replace(
            "<script pret-head-scripts></script>",
            "".join(
                '<script src="{}"></script>'.format(assets_dir + file)
                for file, _ in self.entries
            )
            + f'<script src="{assets_dir + webpack_bundle}"></script>',
        )

        return output

    def on_post_build(self, *, config: MkDocsConfig) -> None:
        for name, file in self.assets.items():
            # index.html is only really used in standalone mode
            # Here, we just use it to get the webpack bundle filename
            if name == "index.html":
                continue
            dest_path = Path(config["site_dir"]) / "assets" / name
            os.makedirs(dest_path.parent, exist_ok=True)
            if isinstance(file, Path):
                shutil.copy(file, dest_path)
            else:
                dest_path.write_text(file)
