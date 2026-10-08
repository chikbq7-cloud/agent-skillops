"""SkillOps API (modified upstream): local-only, persistent, no client secrets."""

import asyncio
import io
import json
import os
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

from adk_optimizer import Agents
from domain import StartInput, SuiteInput, digest, references, skill_content
from dotenv import load_dotenv
from examples import example
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from jobs import ACTIVE, Jobs
from store import Decision, Job, JobEvent, Run, Skill, Store, Suite, Version, Workspace

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
MAX_TOTAL, MAX_FILE, MAX_COUNT = 10 * 1024 * 1024, 1024 * 1024, 50
TEXT_TYPES = {".md", ".txt", ".json", ".yaml", ".yml", ".py", ".js", ".ts", ".toml", ".sh"}


def read_files(entries):
    files, total = {}, 0
    for name, data in entries:
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or any(p.startswith(".") for p in path.parts)
        ):
            raise ValueError("文件路径不安全，不能上传隐藏文件、密钥或目录穿越路径")
        if path.suffix.lower() not in TEXT_TYPES:
            raise ValueError(f"仅接受文本文件：{name}")
        if name in files or len(data) > MAX_FILE:
            raise ValueError("文件名重复或单文件超过 1 MB")
        total += len(data)
        if total > MAX_TOTAL or len(files) >= MAX_COUNT:
            raise ValueError("上传限额：10 MB、50 个文件")
        try:
            files[name] = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("文件必须使用 UTF-8 编码") from error
    skill_content(files)
    return files


def create_app(db_path=None, agents_factory=Agents):
    store = Store(db_path or os.getenv("SKILLOPS_DB", str(ROOT / "data/skillops.db")))
    jobs = Jobs(store, agents_factory)

    @asynccontextmanager
    async def lifespan(app):
        store.interrupt()
        yield
        await jobs.close()
        store.engine.dispose()

    app = FastAPI(title="Agent SkillOps", lifespan=lifespan)
    app.state.store, app.state.jobs = store, jobs
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3001", "http://127.0.0.1:3001"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )

    @app.exception_handler(LookupError)
    async def missing(_, error):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(ValueError)
    async def invalid(_, error):
        return JSONResponse(status_code=400, content={"detail": str(error)})

    def register(files, skill_id=None):
        content, metadata = skill_content(files)
        if skill_id:
            skill = store.get(Skill, skill_id)
            if any(j.status in ACTIVE for j in store.list(Job, skill_id=skill_id)):
                raise ValueError("活动任务期间不能上传新版本")
            if (skill.name, skill.description) != (metadata["name"], metadata["description"]):
                raise ValueError("同一 Skill 的新版本必须保留 name 和 description")
        else:
            skill = Skill(name=metadata["name"], description=metadata["description"])
        version = Version(
            skill_id=skill.id, parent_id=skill.current_version_id, content=content, files=files
        )
        workspace = Workspace(skill_id=skill.id, version_id=version.id)
        store.save(skill, version, workspace)
        return {"session_id": workspace.id, "skill_id": skill.id, "version_id": version.id}

    def save_suite(workspace, body):
        data = body.model_dump(by_alias=True)["cases"]
        suite = Suite(
            skill_id=workspace.skill_id,
            number=len(store.list(Suite, skill_id=workspace.skill_id)) + 1,
            cases=data,
            content_hash=digest(data),
        )
        workspace.suite_id = suite.id
        store.save(suite, workspace)
        return suite.model_dump()

    @app.get("/health")
    @app.get("/api/settings")
    def settings():
        agents = agents_factory()
        return {
            "status": "ok",
            "mode": agents.mode,
            "model": agents.model,
            "key_configured": bool(os.getenv("GOOGLE_API_KEY")),
            "scope": "local-text-skills",
            "max_running": 2,
            "max_waiting": 10,
        }

    @app.post("/api/upload")
    async def upload(files: list[UploadFile] = File(...), skill_id: str | None = Form(None)):
        entries, raw_total = [], 0
        if len(files) > MAX_COUNT:
            raise ValueError("最多上传 50 个文件")
        for file in files:
            data = await file.read(MAX_TOTAL + 1)
            raw_total += len(data)
            if raw_total > MAX_TOTAL:
                raise ValueError("上传文件超过 10 MB")
            if (file.filename or "").endswith(".zip"):
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        members = [m for m in archive.infolist() if not m.is_dir()]
                        if (
                            len(members) + len(entries) > MAX_COUNT
                            or sum(m.file_size for m in members) > MAX_TOTAL
                        ):
                            raise ValueError("压缩包解压后超过限额")
                        for member in members:
                            if (
                                member.file_size > MAX_FILE
                                or (member.external_attr >> 16) & 0o170000 == 0o120000
                            ):
                                raise ValueError("压缩包不能包含超大文件或符号链接")
                            entries.append((member.filename, archive.read(member)))
                except zipfile.BadZipFile as error:
                    raise ValueError("无效 ZIP 文件") from error
            else:
                entries.append((file.filename or "", data))
        return register(read_files(entries), skill_id)

    @app.post("/api/examples/{kind}")
    def load_example(kind: str):
        if agents_factory().mode != "demo":
            raise ValueError("离线样例只在 demo 模式开放；真实模式请上传实际 Skill")
        fixture = example(kind)
        result = register(fixture["files"])
        suite = save_suite(
            store.get(Workspace, result["session_id"]), SuiteInput.model_validate(fixture["suite"])
        )
        return {**result, "suite": suite, "demo_only": True}

    @app.get("/api/skills")
    def list_skills():
        return [s.model_dump() for s in reversed(store.list(Skill))]

    @app.get("/api/skills/{skill_id}")
    def skill_detail(skill_id: str):
        return {
            "skill": store.get(Skill, skill_id).model_dump(),
            "versions": [v.model_dump() for v in store.list(Version, skill_id=skill_id)],
            "suites": [s.model_dump() for s in store.list(Suite, skill_id=skill_id)],
            "jobs": [j.model_dump() for j in store.list(Job, skill_id=skill_id)],
            "decisions": [d.model_dump() for d in store.list(Decision, skill_id=skill_id)],
        }

    @app.post("/api/skills/{skill_id}/resume")
    def resume(skill_id: str):
        skill = store.get(Skill, skill_id)
        versions, suites = (
            store.list(Version, skill_id=skill_id),
            store.list(Suite, skill_id=skill_id),
        )
        workspace = Workspace(
            skill_id=skill_id,
            version_id=skill.current_version_id or versions[-1].id,
            suite_id=suites[-1].id if suites else None,
        )
        store.save(workspace)
        return {"session_id": workspace.id}

    @app.get("/api/sessions/{session_id}")
    def session_detail(session_id: str):
        workspace = store.get(Workspace, session_id)
        return {
            **workspace.model_dump(),
            "skill": store.get(Skill, workspace.skill_id).model_dump(),
            "version": store.get(Version, workspace.version_id).model_dump(),
            "suite": store.get(Suite, workspace.suite_id).model_dump()
            if workspace.suite_id
            else None,
            "job": store.get(Job, workspace.job_id).model_dump() if workspace.job_id else None,
        }

    @app.post("/api/sessions/{session_id}/analyze")
    async def analyze(session_id: str):
        workspace = store.get(Workspace, session_id)
        version = store.get(Version, workspace.version_id)
        agents = agents_factory(4)
        if agents.mode == "demo":
            raise ValueError("离线模式请导入你自己的评测 JSON；预置样例仅用于流程演示")
        prompt = (
            "Design at least 5 train and 3 holdout cases for this specific skill. Include safety checks in both splits and critical functional checks. Use real task inputs, no solutions. Each case has its own criteria.\nSchema:\n"
            + json.dumps(SuiteInput.model_json_schema(), ensure_ascii=False)
            + "\nSkill:\n"
            + version.content
            + "\nReferences:\n"
            + references(version.files)
        )
        body = SuiteInput.model_validate(await agents.json("designer", prompt))
        body.confirm()
        return save_suite(workspace, body)

    @app.post("/api/sessions/{session_id}/suites")
    def new_suite(session_id: str, body: SuiteInput):
        return save_suite(store.get(Workspace, session_id), body)

    @app.post("/api/sessions/{session_id}/config")
    def legacy_config(session_id: str, body: dict):
        # Old UI payloads become drafts; never implicitly confirm them.
        cases = []
        for i, scenario in enumerate(body.get("scenarios", [])):
            checks = [
                {
                    "id": str(e.get("id", f"eval-{n}")),
                    "kind": "llm",
                    "question": e.get("question")
                    or e.get("description")
                    or e.get("name", "是否符合要求？"),
                    "safety": bool(e.get("safety", False)),
                    "critical": bool(e.get("critical", False)),
                }
                for n, e in enumerate(body.get("evals", []))
            ]
            cases.append(
                {
                    "id": str(scenario.get("id", f"case-{i}")),
                    "input": scenario.get("input") or scenario.get("prompt", ""),
                    "split": scenario.get("split", "train" if i < 5 else "holdout"),
                    "checks": checks,
                }
            )
        return save_suite(
            store.get(Workspace, session_id), SuiteInput.model_validate({"cases": cases})
        )

    @app.get("/api/suites/{suite_id}")
    def suite_detail(suite_id: str):
        return store.get(Suite, suite_id).model_dump()

    @app.post("/api/suites/{suite_id}/confirm")
    def confirm(suite_id: str):
        suite = store.get(Suite, suite_id)
        SuiteInput.model_validate({"cases": suite.cases}).confirm()
        if suite.content_hash != digest(suite.cases):
            raise ValueError("评测集完整性校验失败")
        suite.status = "confirmed"
        store.save(suite)
        return suite.model_dump()

    @app.post("/api/sessions/{session_id}/start")
    async def start(session_id: str, body: StartInput):
        if agents_factory().mode != "demo" and not os.getenv("GOOGLE_API_KEY"):
            raise ValueError("请先在服务端 .env 配置 GOOGLE_API_KEY，再启动真实模型任务")
        return jobs.start(store.get(Workspace, session_id), body).model_dump()

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str):
        return {
            **store.get(Job, job_id).model_dump(),
            "runs": [r.model_dump() for r in store.list(Run, job_id=job_id)],
            "events": [e.model_dump() for e in store.list(JobEvent, job_id=job_id)],
        }

    @app.post("/api/jobs/{job_id}/stop")
    async def stop(job_id: str):
        return (await jobs.cancel(job_id)).model_dump()

    @app.get("/api/jobs/{job_id}/events")
    async def events(job_id: str, request: Request, after: int = 0):
        store.get(Job, job_id)
        last = max(after, int(request.headers.get("last-event-id", "0")))

        async def stream():
            nonlocal last
            while not await request.is_disconnected():
                for event in store.list(JobEvent, job_id=job_id):
                    if event.id > last:
                        last = event.id
                        yield f"id: {event.id}\ndata: {json.dumps(event.model_dump(), ensure_ascii=False)}\n\n"
                if store.get(Job, job_id).status not in ACTIVE:
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/skills/{skill_id}/publish/{version_id}")
    def publish(skill_id: str, version_id: str):
        return store.publish(skill_id, version_id)

    @app.post("/api/skills/{skill_id}/rollback/{version_id}")
    def rollback(skill_id: str, version_id: str):
        return store.publish(skill_id, version_id, rollback=True)

    @app.get("/api/versions/{version_id}/download")
    def download(version_id: str):
        version = store.get(Version, version_id)
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, content in version.files.items():
                archive.writestr(path, content)
        data.seek(0)
        return StreamingResponse(
            data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="skill-{version.id[:8]}.zip"'},
        )

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("SKILLOPS_PORT", "8892")))
