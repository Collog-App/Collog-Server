from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from app.api import router
from app.config import Settings, get_settings
from app.container import AppContainer
from app.team_portal import build_team_status, render_team_portal


async def run_maintenance(operation: Callable[[], Awaitable[object]], interval: int) -> None:
    while True:
        try:
            await operation()
        except Exception:
            logging.getLogger(__name__).exception("Maintenance operation failed and will retry")
        await asyncio.sleep(interval)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_runtime()
    settings.ensure_local_directories()
    # uvicorn은 자신의 로거에만 핸들러를 붙이므로 app.* 로거의 INFO는 어디에도 출력되지
    # 않는다. STT 같은 파이프라인 로그를 서버 로그에서 보려면 루트 핸들러가 필요하다.
    logging.getLogger("app").setLevel(logging.INFO)
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s:     %(message)s",
        )
    container = AppContainer(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await container.database.ensure_schema(auto_reset=settings.schema_auto_reset)
        await container.pipeline.release_stale_claims()

        tasks = [
            asyncio.create_task(run_maintenance(operation, settings.maintenance_interval_seconds))
            for operation in (
                container.calls.maintain,
                container.pipeline.process_pending,
                container.pipeline.purge_expired_audio,
                container.report_notifications.maintain,
            )
        ]
        app.state.maintenance_tasks = tasks
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            await container.voip_push.close()
            await container.report_push.close()
            await container.database.close()

    app = FastAPI(
        title="콜록(Collog) API",
        version="0.3.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.state.container = container
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_origins != ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router, prefix=settings.api_prefix)

    @app.get("/team", include_in_schema=False, response_class=HTMLResponse)
    async def team_portal(request: Request) -> HTMLResponse:
        portal_settings = request.app.state.container.settings
        if not portal_settings.team_portal_enabled:
            raise HTTPException(status_code=404, detail="팀 포털이 비활성화되어 있습니다")
        return HTMLResponse(
            render_team_portal(portal_settings),
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
                    "base-uri 'none'; form-action 'none'"
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/team/status.json", include_in_schema=False)
    async def team_portal_status(request: Request) -> dict:
        portal_settings = request.app.state.container.settings
        if not portal_settings.team_portal_enabled:
            raise HTTPException(status_code=404, detail="팀 포털이 비활성화되어 있습니다")
        return build_team_status(portal_settings)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        del request
        code_by_status = {
            401: "UNAUTHORIZED",
            403: "SCOPE_DENIED",
            404: "NOT_FOUND",
            409: "CONFLICT",
            410: "INVITE_EXPIRED",
            422: "VALIDATION_ERROR",
            429: "TOO_MANY_REQUESTS",
            502: "UPSTREAM_ERROR",
            503: "SERVICE_UNAVAILABLE",
        }
        message = exc.detail if isinstance(exc.detail, str) else "요청을 처리하지 못했습니다"
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": code_by_status.get(exc.status_code, "REQUEST_FAILED"),
                "message": message,
            },
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        del request
        first = exc.errors()[0] if exc.errors() else {}
        message = str(
            first.get("ctx", {}).get("error") or first.get("msg", "입력값을 확인해주세요")
        )
        return JSONResponse(
            status_code=422,
            content={"code": "VALIDATION_ERROR", "message": message},
        )

    return app


app = create_app()
