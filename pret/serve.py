import asyncio
import inspect
import mimetypes
from pathlib import Path
from typing import Dict, Union

from quart import (
    Quart,
    Response,
    abort,
    request,
    send_file,
    send_from_directory,
    websocket,
)

from pret.manager import get_manager


def make_app(assets: Dict[str, Union[str, Path]]) -> Quart:
    app = Quart(__name__)

    manager = get_manager()

    websocket_id = 0

    @app.websocket("/ws")
    async def ws() -> None:
        nonlocal websocket_id
        websocket_id += 1

        send_queue = manager.register_connection(websocket_id)

        async def _send():
            while True:
                await websocket.send_json(await send_queue.get())

        task = None
        try:
            task = asyncio.ensure_future(_send())

            while True:
                message = manager.handle_websocket_msg(
                    await websocket.receive_json(),
                    websocket_id,
                )
                await websocket.send_json(message)
        finally:
            if task is not None:
                task.cancel()
            manager.unregister_connection(websocket_id)

    @app.route("/method", methods=["GET", "POST"])
    async def method_route():
        body = await request.get_json()
        method, data = body["method"], body["data"]
        result = manager.handle_message(method, data)
        if inspect.isawaitable(result):
            result = await result
        if result is not None:
            return {"method": result[0], "data": result[1]}
        else:
            return {}

    @app.route("/", defaults={"path": "index.html"})
    @app.route("/<path:path>")
    async def page_route(path):
        mimetype = mimetypes.guess_type(path)[0]
        if path in assets:
            asset = assets[path]
            if isinstance(asset, Path):
                return await send_file(asset, mimetype=mimetype)
            elif isinstance(asset, str):
                return Response(asset, mimetype=mimetype)
            elif isinstance(asset, bytes):
                return Response(asset, mimetype="application/octet-stream")
        else:
            try:
                return await send_from_directory(assets["*"], path)
            except (KeyError, FileNotFoundError):
                abort(404)  # If there's no default file, return a 404 error

    return app
