"""FastAPI application factory for the LangGraph RCA service."""

import logging
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from aiops_rca.api.models import InvestigationApiRequest, InvestigationApiResponse
from aiops_rca.config.settings import Settings
from aiops_rca.services.investigation import InvestigationService, build_live_service

LOGGER = logging.getLogger(__name__)


def create_app(
    *,
    settings: Settings | None = None,
    service: InvestigationService | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()
    resolved_service = service or build_live_service(resolved_settings)
    expected_token = resolved_settings.aiops_internal_token.get_secret_value()

    app = FastAPI(title="AIOps LangGraph RCA API", version="0.1.0")
    app.state.service = resolved_service

    async def require_internal_token(
        x_aiops_internal_token: Annotated[str | None, Header()] = None,
    ) -> None:
        if x_aiops_internal_token != expected_token:
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ready"}

    @app.post(
        "/v1/investigations",
        response_model=InvestigationApiResponse,
        dependencies=[Depends(require_internal_token)],
    )
    async def investigate(
        body: InvestigationApiRequest,
        request: Request,
    ) -> InvestigationApiResponse:
        return await request.app.state.service.investigate(body)

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, error: Exception) -> JSONResponse:
        LOGGER.exception("RCA investigation failed", exc_info=error)
        return JSONResponse(
            status_code=500,
            content={"error": "investigation_failed", "message": str(error)[:1000]},
        )

    return app


def run() -> None:
    settings = Settings()
    uvicorn.run(
        "aiops_rca.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )
