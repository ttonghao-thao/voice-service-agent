"""Install the phase-1 single-tool catalog using the existing CAS/admin audit.

Run inside the API container after migrations, before accepting voice sessions.
Only --apply writes. No application lifespan, recovery, model call or secret dump.
"""

import argparse
import asyncio
import json

import httpx
from app.agent_runtime.runtime import BusinessRuntime
from app.config import Settings
from app.storage.store import Store
from app.tools.registry import ToolRegistry


async def configure(settings, apply=False):
    store = Store(settings.database_url)
    runtime = None
    try:
        async with httpx.AsyncClient() as client:
            runtime = BusinessRuntime(settings, ToolRegistry(settings, client, store))
            revision, current = await runtime.catalog.configuration()
            target = runtime.catalog.load_file()
            if len(target) != 1 or (
                target[0].name, target[0].implementation
            ) != ("search_knowledge_base", "search_cuekb"):
                raise ValueError(
                    "The checked-in phase-1 catalog must expose only search_knowledge_base via search_cuekb"
                )
            changed = [entry.model_dump() for entry in current] != [entry.model_dump() for entry in target]
            if apply and changed:
                revision = await runtime.catalog.update(target, revision, "deployment",
                    action="native_tools.cuekb_phase1")
            return {"applied": apply, "changed": changed,
                    "previous_names": [entry.name for entry in current],
                    "target_names": [entry.name for entry in target], "revision": revision}
    finally:
        if runtime and runtime.client:
            await runtime.client.close()
        await store.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Replace the persisted native catalog with the single-tool profile")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(configure(Settings(), args.apply)), indent=2))
