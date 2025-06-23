import asyncio

import pytest

from pret.store import create_store


@pytest.mark.asyncio
async def test_sync_two_states_update(tmp_path):
    path = tmp_path / "state.bin"
    state1 = create_store({"value": 0}, sync=path)
    state2 = create_store({"value": 0}, sync=path)
    assert state1.doc.sync_id == state2.doc.sync_id

    await asyncio.sleep(0.01)

    state1["value"] = 1
    await asyncio.sleep(0.1)
    await asyncio.sleep(0.1)
    assert state1["value"] == 1
    assert state2["value"] == 1

    state2["value"] = 2
    await asyncio.sleep(0.5)
    assert state1["value"] == 2

    state1.doc._persistence_watcher.cancel()
    state2.doc._persistence_watcher.cancel()
    await asyncio.sleep(0)
    state1.doc._persistence_finalizer.detach()
    state2.doc._persistence_finalizer.detach()
    state1.doc._persistence_finalizer = None
    state2.doc._persistence_finalizer = None


@pytest.mark.asyncio
async def test_hydrate_from_existing_file(tmp_path):
    path = tmp_path / "persist.bin"
    state1 = create_store({"count": 0}, sync=path)
    state1["count"] = 7
    await asyncio.sleep(0.05)
    state1.doc._persistence_watcher.cancel()
    await asyncio.sleep(0)
    state1.doc._persistence_finalizer.detach()
    state1.doc._persistence_finalizer = None

    state2 = create_store({}, sync=path)
    assert state2.doc.sync_id == state1.doc.sync_id
    await asyncio.sleep(0.05)
    assert state2["count"] == 7
    state2.doc._persistence_watcher.cancel()
    await asyncio.sleep(0)
    state2.doc._persistence_finalizer.detach()
    state2.doc._persistence_finalizer = None
