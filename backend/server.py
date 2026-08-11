"""FastAPI backend server for the AI Engineering Studio."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.paths import ProjectStateCleanupError, remove_project_state
from .websocket import WebSocketManager
from orchestrator.engine import Engine
from storage.repositories import (
    ProjectRepository, JobRepository, TaskRepository, ReviewRepository
)

logger = logging.getLogger(__name__)

# ── Request/Response Models ──

class ProjectCreate(BaseModel):
    name: str
    root_path: str
    description: str = ""

class JobCreate(BaseModel):
    project_id: int
    user_request: str
    risk_level: str = "medium"
    source_job_id: str | None = None
    attachments: list[dict] = Field(default_factory=list)

class JobAction(BaseModel):
    action: str  # pause, resume, cancel


# ── App Factory ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting AI Engineering Studio backend...")
    await app.state.engine.start()
    yield
    logger.info("Shutting down AI Engineering Studio backend...")
    await app.state.engine.stop()


def create_app(engine: Engine | None = None) -> FastAPI:
    if engine is None:
        engine = Engine()

    app = FastAPI(title="AI Engineering Studio API", lifespan=lifespan)
    app.state.engine = engine
    app.state.ws_manager = WebSocketManager()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Wire engine events to WebSocket ──
    @engine.event_bus.subscribe("*")
    async def on_event(event_type: str, **data):
        await app.state.ws_manager.broadcast_event(event_type, **data)

    # ── REST Endpoints ──

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "running": app.state.engine._running}

    # ── Projects ──

    @app.get("/api/projects")
    async def list_projects():
        repos = _get_repos(app)
        try:
            projects = repos["project"].list_all()
            return [
                {"id": p.id, "name": p.name, "root_path": p.root_path,
                 "description": p.description, "created_at": str(p.created_at)}
                for p in projects
            ]
        finally:
            _close_repos(repos)

    @app.post("/api/projects")
    async def create_project(data: ProjectCreate):
        repos = _get_repos(app)
        try:
            project = repos["project"].create(
                name=data.name, root_path=data.root_path,
                description=data.description
            )
            return {"id": project.id, "name": project.name, "root_path": project.root_path}
        finally:
            _close_repos(repos)

    @app.delete("/api/projects/{project_id}")
    async def delete_project(project_id: int):
        repos = _get_repos(app)
        try:
            project = repos["project"].get_by_id(project_id)
            if not project:
                raise HTTPException(status_code=404, detail="Project not found")
            try:
                removed_state = remove_project_state(project.root_path)
            except ProjectStateCleanupError as error:
                raise HTTPException(
                    status_code=409,
                    detail=f"Project state cleanup failed: {error}",
                ) from error
            success = repos["project"].delete(project_id)
            if not success:
                raise HTTPException(status_code=404, detail="Project not found")
            return {
                "status": "deleted",
                "removed_state_directories": len(removed_state),
            }
        finally:
            _close_repos(repos)

    # ── Jobs ──

    @app.get("/api/projects/{project_id}/jobs")
    async def list_jobs(project_id: int):
        repos = _get_repos(app)
        try:
            jobs = repos["job"].list_by_project(project_id)
            return [
                {"job_id": j.job_id, "status": j.status, "user_request": j.user_request[:200],
                 "source_job_id": j.source_job_id,
                 "risk_level": j.risk_level, "created_at": str(j.created_at)}
                for j in jobs
            ]
        finally:
            _close_repos(repos)

    @app.post("/api/jobs")
    async def create_job(data: JobCreate):
        engine = app.state.engine
        result = await engine.create_job(
            project_id=data.project_id,
            user_request=data.user_request,
            project_root="",
            risk_level=data.risk_level,
            source_job_id=data.source_job_id,
            attachments=data.attachments,
        )
        return result

    @app.post("/api/jobs/{job_id}/run")
    async def run_job(job_id: str):
        engine = app.state.engine
        repos = _get_repos(app)
        try:
            job = repos["job"].get_by_id(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            asyncio.ensure_future(engine.run_job(job_id, job.project.root_path))
            return {"status": "started", "job_id": job_id}
        finally:
            _close_repos(repos)

    @app.post("/api/jobs/{job_id}/action")
    async def job_action(job_id: str, action: JobAction):
        engine = app.state.engine
        if action.action == "pause":
            await engine.pause_job(job_id)
        elif action.action == "resume":
            await engine.resume_job(job_id)
        elif action.action == "cancel":
            await engine.cancel_job(job_id)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown action: {action.action}")
        return {"status": action.action, "job_id": job_id}

    # ── Tasks ──

    @app.get("/api/jobs/{job_id}/tasks")
    async def list_tasks(job_id: str):
        repos = _get_repos(app)
        try:
            job = repos["job"].get_by_id(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            tasks = repos["task"].list_by_job(job.id)
            return [
                {"task_id": t.task_id, "title": t.title, "type": t.task_type,
                 "status": t.status, "dependencies": t.dependencies,
                 "allowed_paths": t.allowed_paths}
                for t in tasks
            ]
        finally:
            _close_repos(repos)

    # ── Reviews ──

    @app.get("/api/jobs/{job_id}/reviews")
    async def list_reviews(job_id: str):
        repos = _get_repos(app)
        try:
            job = repos["job"].get_by_id(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            reviews = repos["review"].list_by_job(job.id)
            return [
                {"result": r.result, "severity": r.severity,
                 "issues": r.issues, "summary": r.summary,
                 "created_at": str(r.created_at)}
                for r in reviews
            ]
        finally:
            _close_repos(repos)

    # ── Configuration ──

    @app.get("/api/config")
    async def get_config():
        from app.ui.settings_dialog import load_config
        return load_config()

    @app.post("/api/config")
    async def save_config(config: dict):
        from app.ui.settings_dialog import save_config
        save_config(config)
        return {"status": "saved"}

    # ── WebSocket ──

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        manager = app.state.ws_manager
        await manager.connect(websocket)
        try:
            while True:
                data = await websocket.receive_text()
                # Client can send ping/pong or commands
                if data == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
        except WebSocketDisconnect:
            await manager.disconnect(websocket)
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            await manager.disconnect(websocket)

    return app


def _get_repos(app):
    from storage.database import create_session_factory
    session = create_session_factory(app.state.engine._engine)()
    return {
        "project": ProjectRepository(session),
        "job": JobRepository(session),
        "task": TaskRepository(session),
        "review": ReviewRepository(session),
        "_session": session,
    }


def _close_repos(repos):
    repos["_session"].close()
