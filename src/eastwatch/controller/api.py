import asyncio
import time
from collections.abc import Callable
from functools import partial
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator, model_validator

from eastwatch.controller.models import (
    ActiveLease,
    Completion,
    RunReferences,
    StaleLeaseError,
    UnknownWorkspaceError,
)
from eastwatch.controller.store import ControllerStore


class ClaimRequest(BaseModel):
    free_slots: int = Field(ge=0, le=10)
    wait_seconds: float = Field(default=20, ge=0, le=25)


class ClaimedJobBody(BaseModel):
    envelope: dict[str, Any]
    lease_generation: int
    lease_until: float


class ClaimResponse(BaseModel):
    jobs: list[ClaimedJobBody]


class RunReferencesBody(BaseModel):
    run_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    tmux_session: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    worktree_relpath: str
    run_dir_relpath: str
    session_file_relpath: str | None = None

    @field_validator("worktree_relpath", "run_dir_relpath", "session_file_relpath")
    @classmethod
    def relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value in ("", "."):
            raise ValueError(
                "artifact references must be non-empty relative paths without '..'"
            )
        return str(path)

    def to_domain(self) -> RunReferences:
        return RunReferences(**self.model_dump())


class StartedRequest(BaseModel):
    lease_generation: int = Field(ge=1)
    refs: RunReferencesBody


class StartedResponse(BaseModel):
    accepted: bool


class LeaseBody(BaseModel):
    job_id: str
    lease_generation: int = Field(ge=1)


class HeartbeatRequest(BaseModel):
    active: list[LeaseBody] = Field(max_length=10)


class CompletionRequest(BaseModel):
    lease_generation: int = Field(ge=1)
    state: Literal["succeeded", "failed"]
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    @model_validator(mode="after")
    def completion_shape(self) -> "CompletionRequest":
        if self.state == "failed" and self.error is None:
            raise ValueError("failed completion requires error")
        if self.state == "succeeded" and self.result is None:
            raise ValueError("succeeded completion requires result")
        return self


class CompletionResponse(BaseModel):
    accepted: bool


class PrestartFailureRequest(BaseModel):
    lease_generation: int = Field(ge=1)
    error: dict[str, Any]


class ActionClaimRequest(BaseModel):
    limit: int = Field(default=5, ge=0, le=5)


class ActionFinishRequest(BaseModel):
    lease_generation: int = Field(ge=1)
    success: bool
    error: str | None = Field(default=None, max_length=1000)


def register_action_routes(app, store, settings, workspace_dependency, clock) -> None:
    @app.post("/v1/runner/actions/claim")
    async def claim_actions(
        request: ActionClaimRequest,
        workspace_id: Annotated[str, Depends(workspace_dependency)],
    ) -> dict[str, object]:
        actions = await run_in_threadpool(
            partial(
                store.claim_workspace_actions,
                workspace_id,
                request.limit,
                settings.lease_seconds,
                clock(),
            )
        )
        return {"actions": list(actions)}

    @app.post(
        "/v1/runner/actions/{action_id}/finish", response_model=CompletionResponse
    )
    async def finish_action(
        action_id: str,
        request: ActionFinishRequest,
        workspace_id: Annotated[str, Depends(workspace_dependency)],
    ) -> CompletionResponse:
        try:
            accepted = await run_in_threadpool(
                partial(
                    store.finish_workspace_action,
                    workspace_id,
                    action_id,
                    request.lease_generation,
                    success=request.success,
                    error=request.error,
                    now=clock(),
                )
            )
        except StaleLeaseError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
        return CompletionResponse(accepted=accepted)


class ApiSettings(BaseModel):
    lease_seconds: float = Field(default=90, ge=30, le=600)
    long_poll_seconds: float = Field(default=20, ge=0, le=25)


def create_app(
    store: ControllerStore,
    settings: ApiSettings,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    app = FastAPI(title="eastwatch controller", docs_url=None, redoc_url=None)
    bearer = HTTPBearer(auto_error=False)

    async def workspace(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        if credentials is None or credentials.scheme.casefold() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="runner token required"
            )
        try:
            row = await run_in_threadpool(
                store.authenticate_workspace, credentials.credentials
            )
        except UnknownWorkspaceError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error)
            ) from error
        return str(row["workspace_id"])

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/runner/claim", response_model=ClaimResponse)
    async def claim(
        request: ClaimRequest, workspace_id: Annotated[str, Depends(workspace)]
    ) -> ClaimResponse:
        deadline = clock() + min(request.wait_seconds, settings.long_poll_seconds)
        while True:
            jobs = await run_in_threadpool(
                partial(
                    store.claim_jobs,
                    workspace_id,
                    request.free_slots,
                    settings.lease_seconds,
                    clock(),
                )
            )
            if jobs or request.free_slots == 0 or clock() >= deadline:
                return ClaimResponse(
                    jobs=[
                        ClaimedJobBody(
                            envelope=job.envelope.to_dict(),
                            lease_generation=job.lease_generation,
                            lease_until=job.lease_until,
                        )
                        for job in jobs
                    ]
                )
            await asyncio.sleep(min(1.0, max(0.0, deadline - clock())))

    @app.post("/v1/runner/jobs/{job_id}/started", response_model=StartedResponse)
    async def started(
        job_id: str,
        request: StartedRequest,
        workspace_id: Annotated[str, Depends(workspace)],
    ) -> StartedResponse:
        try:
            accepted = await run_in_threadpool(
                partial(
                    store.mark_started,
                    workspace_id,
                    job_id,
                    request.lease_generation,
                    request.refs.to_domain(),
                    clock(),
                )
            )
        except StaleLeaseError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
        return StartedResponse(accepted=accepted)

    @app.post("/v1/runner/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
    async def heartbeat(
        request: HeartbeatRequest,
        workspace_id: Annotated[str, Depends(workspace)],
    ) -> None:
        active = tuple(ActiveLease(**lease.model_dump()) for lease in request.active)
        try:
            await run_in_threadpool(
                partial(
                    store.heartbeat,
                    workspace_id,
                    active,
                    settings.lease_seconds,
                    clock(),
                )
            )
        except StaleLeaseError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error

    @app.post(
        "/v1/runner/jobs/{job_id}/failed-before-start",
        response_model=CompletionResponse,
    )
    async def failed_before_start(
        job_id: str,
        request: PrestartFailureRequest,
        workspace_id: Annotated[str, Depends(workspace)],
    ) -> CompletionResponse:
        try:
            accepted = await run_in_threadpool(
                partial(
                    store.fail_unstarted,
                    workspace_id,
                    job_id,
                    request.lease_generation,
                    request.error,
                    clock(),
                )
            )
        except StaleLeaseError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
        return CompletionResponse(accepted=accepted)

    @app.post("/v1/runner/ready", status_code=status.HTTP_204_NO_CONTENT)
    async def mark_ready(workspace_id: Annotated[str, Depends(workspace)]) -> None:
        await run_in_threadpool(
            partial(store.mark_workspace_ready, workspace_id, clock())
        )

    @app.post("/v1/runner/jobs/{job_id}/complete", response_model=CompletionResponse)
    async def complete(
        job_id: str,
        request: CompletionRequest,
        workspace_id: Annotated[str, Depends(workspace)],
    ) -> CompletionResponse:
        try:
            accepted = await run_in_threadpool(
                partial(
                    store.complete_job,
                    workspace_id,
                    job_id,
                    request.lease_generation,
                    Completion(
                        state=request.state, result=request.result, error=request.error
                    ),
                    clock(),
                )
            )
        except StaleLeaseError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
        return CompletionResponse(accepted=accepted)

    register_action_routes(app, store, settings, workspace, clock)
    return app
