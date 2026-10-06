"""
Check Python serialization and the JavaScript requested by browser views
"""

import gc
import threading
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import cbor2
import pytest

from pret.manager import JupyterServerManager
from pret.marshal import (
    COVERAGE,
    PretMarshaler,
    clear_shared_marshaler,
    get_shared_marshaler,
    make_stub_js_module,
)


def test_stub_components_resolve_through_runtime_registry():
    """
    Load the React component registered for a serialized Python component
    """
    module = make_stub_js_module(
        "TestComponents",
        "test-components-python",
        "test-components",
        "1.0.0",
        __name__,
    )

    (_, code), _ = PretMarshaler().dump(module.Table)

    assert "pret_modules.components" in code
    assert "window[global_name]" in code
    assert "resolve_stub_component" in code
    assert "pret_modules.js.TestComponents.Table" not in code


def test_shared_marshaler_survives_delayed_bundle_request():
    """
    Keep the serializer alive until the browser has requested its JavaScript code
    """
    clear_shared_marshaler()
    marshaler_id = get_shared_marshaler().id

    gc.collect()

    assert get_shared_marshaler().id == marshaler_id
    clear_shared_marshaler()


def test_bundle_response_during_concurrent_append(monkeypatch):
    """
    Keep response metadata and cached bytes consistent when a callback appends during compilation
    """
    marshaler = PretMarshaler()
    marshaler.append(1)
    compiling = threading.Event()
    release = threading.Event()
    appending = threading.Event()
    appended = threading.Event()

    def build(blob):
        # Pause compilation so the callback can attempt to append before the response is ready
        compiling.set()
        assert release.wait(5)
        return blob, ""

    def append():
        appending.set()
        marshaler.append(2)
        appended.set()

    monkeypatch.setattr(marshaler, "_build_bundle", build)
    monkeypatch.setattr("pret.marshal.get_shared_marshaler", lambda: marshaler)
    request = {"marshaler_id": marshaler.id, "request_id": "test", "byte_offset": 0}
    with ThreadPoolExecutor(max_workers=2) as workers:
        response = workers.submit(JupyterServerManager.handle_bundle_request, None, request)
        try:
            assert compiling.wait(5)
            callback = workers.submit(append)
            assert appending.wait(5)
            appended.wait(0.1)
        finally:
            release.set()
        first = response.result(timeout=5)
        callback.result(timeout=5)

    assert first[1]["max_chunk_idx"] == 0
    assert bytes(first[3][0]) == cbor2.dumps(1)
    second = JupyterServerManager.handle_bundle_request(None, request)
    assert second[1]["max_chunk_idx"] == 1
    stream = BytesIO(second[3][0])
    assert [cbor2.load(stream), cbor2.load(stream)] == [1, 2]
    assert stream.read() == b""


def test_updated_source_requires_restarting_the_loaded_function(tmp_path):
    """
    Report changed package source when serializing a view from a running kernel
    """
    source = tmp_path / "recipe.py"
    source.write_text("def filter_related_tables():\n    return 1\n")
    namespace = {"__name__": "recipe"}
    exec(compile(source.read_text(), str(source), "exec"), namespace)
    loaded = namespace["filter_related_tables"]
    source.write_text("def query_from():\n    return 2\n")

    def render():
        return loaded()

    with pytest.raises(ValueError, match="Restart the kernel or Python process"):
        PretMarshaler().dump({"view": render})

    exec(compile(source.read_text(), str(source), "exec"), namespace)
    loaded = namespace["query_from"]
    (_, code), _ = PretMarshaler().dump({"view": render})
    assert "return query_from;" in code


@pytest.mark.skipif(COVERAGE, reason="coverage runs keep one factory per function to attribute lines to its source")
def test_view_rendered_again_reuses_its_browser_code(monkeypatch):
    """
    Keep the JavaScript of a view rendered again in the same kernel, its closures reuse their factories
    """
    marshaler = PretMarshaler()
    builds = []
    build_bundle = marshaler._build_bundle

    def build(blob):
        builds.append(len(marshaler.source_codes))
        return build_bundle(blob)

    def make_view(value):
        def view():
            return value

        return view

    monkeypatch.setattr(marshaler, "_build_bundle", build)
    marshaler.append(make_view(1))
    _, code = marshaler.get_serialized_bytes()
    marshaler.append(make_view(2))
    blob, again = marshaler.get_serialized_bytes()

    assert again == code
    assert builds == [1]
    stream = BytesIO(blob)
    assert cbor2.load(stream).value[1] == [1]
    assert cbor2.load(stream).value[1] == [2]


@pytest.mark.asyncio
async def test_standalone_method_route_sends_binary_replies():
    """
    The standalone /method route frames bytes returned by a server function after their JSON message
    """
    import json

    from pret.manager import BinaryPayload, StandaloneServerManager
    from pret.serve import make_app

    manager = StandaloneServerManager()
    StandaloneServerManager.manager, previous = manager, StandaloneServerManager.manager
    try:
        manager.register_function(lambda: b"%PDF-1.4", "pdf")
        manager.register_function(lambda: BinaryPayload({"rows": 1}, [b"arrow"]), "page")
        client = make_app({"index.html": ""}).test_client()
        replies = {}
        for function_id in ("pdf", "page"):
            response = await client.post(
                "/method",
                json={
                    "method": "call",
                    "data": {"function_id": function_id, "args": [], "kwargs": {}, "callback_id": "1"},
                },
            )
            assert response.headers["X-Pret-Binary"] == "1"
            body = await response.get_data()
            length = int.from_bytes(body[:4], "big")
            replies[function_id] = json.loads(body[4 : 4 + length])["data"]["value"], body[4 + length :]
    finally:
        StandaloneServerManager.manager = previous
    assert replies["pdf"] == ({"__pret_binary__": True, "__pret_bytes__": True}, b"%PDF-1.4")
    assert replies["page"] == ({"__pret_binary__": True, "rows": 1}, b"arrow")
