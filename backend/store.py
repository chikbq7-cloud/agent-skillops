"""Small SQLite repository. Each write uses a short, independent transaction."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import JSON, Column, event
from sqlmodel import Field, Session, SQLModel, create_engine, select


def uid() -> str:
    return uuid4().hex


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Skill(SQLModel, table=True):
    id: str = Field(default_factory=uid, primary_key=True)
    name: str
    description: str
    current_version_id: str | None = None
    created_at: str = Field(default_factory=now)


class Version(SQLModel, table=True):
    id: str = Field(default_factory=uid, primary_key=True)
    skill_id: str = Field(index=True)
    parent_id: str | None = None
    content: str
    files: dict = Field(sa_column=Column(JSON))
    status: str = "draft"
    summary: str = "上传原始版本"
    created_at: str = Field(default_factory=now)


class Suite(SQLModel, table=True):
    id: str = Field(default_factory=uid, primary_key=True)
    skill_id: str = Field(index=True)
    number: int
    cases: list = Field(sa_column=Column(JSON))
    content_hash: str
    status: str = "draft"
    created_at: str = Field(default_factory=now)


class Workspace(SQLModel, table=True):
    id: str = Field(default_factory=uid, primary_key=True)
    skill_id: str
    version_id: str
    suite_id: str | None = None
    job_id: str | None = None


class Run(SQLModel, table=True):
    id: str = Field(default_factory=uid, primary_key=True)
    job_id: str = Field(index=True)
    version_id: str
    suite_id: str
    split: str
    report: dict = Field(sa_column=Column(JSON))
    created_at: str = Field(default_factory=now)


class Job(SQLModel, table=True):
    id: str = Field(default_factory=uid, primary_key=True)
    skill_id: str = Field(index=True)
    session_id: str
    version_id: str
    suite_id: str
    status: str = "queued"
    error: str | None = None
    result: dict | None = Field(default=None, sa_column=Column(JSON))
    created_at: str = Field(default_factory=now)


class JobEvent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: str = Field(index=True)
    type: str
    data: dict = Field(sa_column=Column(JSON))
    created_at: str = Field(default_factory=now)


class Decision(SQLModel, table=True):
    id: str = Field(default_factory=uid, primary_key=True)
    skill_id: str = Field(index=True)
    version_id: str
    job_id: str | None = None
    action: str
    reason: str
    previous_id: str | None = None
    created_at: str = Field(default_factory=now)


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 15}
        )

        @event.listens_for(self.engine, "connect")
        def configure(dbapi, _):
            dbapi.execute("PRAGMA journal_mode=WAL")

        SQLModel.metadata.create_all(self.engine)

    def get(self, model, key):
        with Session(self.engine) as session:
            result = session.get(model, key)
            if result is None:
                raise LookupError(f"{model.__name__} 不存在")
            return result

    def list(self, model, **filters):
        with Session(self.engine) as session:
            query = select(model)
            for key, value in filters.items():
                query = query.where(getattr(model, key) == value)
            order = (
                model.id
                if model is JobEvent
                else model.created_at
                if hasattr(model, "created_at")
                else model.id
            )
            return list(session.exec(query.order_by(order)).all())

    def save(self, *records):
        with Session(self.engine, expire_on_commit=False) as session:
            for record in records:
                session.add(record)
            session.commit()
            for record in records:
                session.refresh(record)

    def emit(self, job_id: str, type_: str, data: dict):
        self.save(JobEvent(job_id=job_id, type=type_, data=data))

    def interrupt(self):
        for job in self.list(Job):
            if job.status in {"queued", "running", "stopping"}:
                job.status, job.error = "interrupted", "服务重启，任务需要重新启动"
                self.save(job)
                self.emit(job.id, "interrupted", {"message": job.error})

    def publish(self, skill_id: str, version_id: str, rollback: bool = False):
        # Version checks and pointer update belong to the same transaction.
        with Session(self.engine) as session:
            skill = session.get(Skill, skill_id)
            version = session.get(Version, version_id)
            if not skill or not version or version.skill_id != skill_id:
                raise LookupError("版本不属于这个 Skill")
            if session.exec(
                select(Job).where(
                    Job.skill_id == skill_id, Job.status.in_(["queued", "running", "stopping"])
                )
            ).first():
                raise ValueError("活动优化任务期间不能发布或回滚")
            history = list(
                session.exec(
                    select(Decision).where(
                        Decision.skill_id == skill_id, Decision.version_id == version_id
                    )
                ).all()
            )
            if rollback:
                if not any(d.action in {"publish", "rollback"} for d in history):
                    raise ValueError("只能回滚到历史发布版本")
            else:
                approvals = [d for d in history if d.action == "approved" and d.job_id]
                if version.status != "approved" or not approvals:
                    raise ValueError("只能发布已通过验证的新候选；历史版本请使用回滚")
                job = session.get(Job, sorted(approvals, key=lambda d: d.created_at)[-1].job_id)
                if (
                    not job
                    or job.status != "completed"
                    or not job.result
                    or job.result.get("decision") != "approved"
                ):
                    raise ValueError("发布验证证据不完整")
                if job.result.get("published_at_start") != skill.current_version_id:
                    raise ValueError("当前发布版本已变化，请重新验证候选")
            decision = Decision(
                skill_id=skill_id,
                version_id=version_id,
                action="rollback" if rollback else "publish",
                reason="用户回滚" if rollback else "用户发布",
                previous_id=skill.current_version_id,
            )
            skill.current_version_id = version_id
            version.status = "released"
            session.add_all([skill, version, decision])
            session.commit()
            return decision.model_dump()
