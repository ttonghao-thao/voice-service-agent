import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.agent_runtime.runtime import BusinessRuntime
from app.api.auth import Auth
from app.api.logging import configure_logging
from app.api.routes import capabilities, router
from app.config import Settings
from app.contracts import DomainError
from app.sessions.coordination import Coordination
from app.sessions.coordinator import SessionCoordinator
from app.storage.store import Store
from app.tools.registry import ToolRegistry
from app.voice.gateway import VoiceGateway


def create_app(settings=None):
    injected_settings = settings is not None
    settings = settings or Settings()
    configure_logging()

    @asynccontextmanager
    async def lifespan(app):
        if not injected_settings:
            settings.validate_deployment()
        async with AsyncExitStack() as cleanup:
            store = Store(settings.database_url)
            auth = Auth(settings, store)
            cleanup.push_async_callback(store.engine.dispose)
            client = await cleanup.enter_async_context(
                httpx.AsyncClient(timeout=5, follow_redirects=False, limits=httpx.Limits(max_connections=50))
            )
            coordination = Coordination(settings)
            await coordination.start()
            cleanup.push_async_callback(coordination.close)
            if settings.auto_create_schema:
                await store.init_dev()
            await store.healthy()
            registry = ToolRegistry(settings, client, store)
            runtime = BusinessRuntime(settings, registry)
            if runtime.client:
                cleanup.push_async_callback(runtime.client.close)
            coordinator = SessionCoordinator(store, runtime, coordination, settings)
            cleanup.push_async_callback(coordinator.close)
            voice = VoiceGateway(settings, coordinator, store)
            coordinator.voice = voice
            app.state.settings, app.state.store, app.state.client = settings, store, client
            app.state.auth, app.state.registry = auth, registry
            app.state.coordinator, app.state.coordination, app.state.voice = coordinator, coordination, voice
            await coordinator.recover()

            async def maintenance():
                last_purge = 0
                while True:
                    await asyncio.sleep(5)
                    voice.expire()
                    last_purge += 5
                    if last_purge >= 3600:
                        await store.purge(settings.retention_days)
                        last_purge = 0
                    if not coordination.valid:
                        await coordinator.close()
                        return

            maintenance_task = asyncio.create_task(maintenance())
            try:
                await store.purge(settings.retention_days)
                yield
            finally:
                maintenance_task.cancel()
                await asyncio.gather(maintenance_task, return_exceptions=True)

    app = FastAPI(
        title="Customer support portal",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        if request.method in ("POST", "PATCH", "DELETE", "PUT"):
            origin = request.headers.get("origin")
            if origin and origin != settings.public_origin:
                return JSONResponse({"code": "FORBIDDEN", "message": "Request origin is not trusted"}, status_code=403)
        try:
            length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            length = 16385
        if length > 16384:
            return JSONResponse({"code": "REQUEST_TOO_LARGE", "message": "Request body is too large"}, status_code=413)
        if request.method in ("POST", "PATCH", "DELETE", "PUT"):
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 16384:
                    return JSONResponse(
                        {"code": "REQUEST_TOO_LARGE", "message": "Request body is too large"}, status_code=413
                    )
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(exc.payload(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"code": "INVALID_REQUEST", "message": "Request does not match the API contract"}, status_code=422)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request):
        try:
            await request.app.state.store.healthy()
            await request.app.state.coordination.check()
            if request.app.state.coordinator.draining:
                raise ValueError("draining")
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        caps = capabilities(settings)
        return {
            "status": "ready" if caps["text_configured"] else "degraded",
            "text_configured": caps["text_configured"],
            "voice_configured": caps["voice_available"],
            "is_mock": caps["is_mock"],
            "enabled_tools": caps["enabled_tools"],
            "voice_capacity_available": len(request.app.state.voice.sessions) < settings.max_voice_sessions,
        }

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return JSONResponse(
            DomainError("INTERNAL_ERROR", "The service is temporarily unavailable. Please try again later.", 500, True).payload(),
            status_code=500,
        )

    app.include_router(router)
    return app


app = create_app()
