"""Single-process task supervision and durable evaluation/release evidence."""

import asyncio
import contextlib

from adk_optimizer import Agents, SkillOptimizer
from domain import digest, references, replace_skill
from evaluation import Evaluator, release_gate
from store import Decision, Job, Run, Skill, Suite, Version

ACTIVE = {"queued", "running", "stopping"}


class Jobs:
    def __init__(self, store, agents_factory=Agents):
        self.store, self.agents_factory = store, agents_factory
        self.tasks = {}
        self.slots = asyncio.Semaphore(2)

    def start(self, workspace, options):
        active = [j for j in self.store.list(Job) if j.status in ACTIVE]
        if any(j.skill_id == workspace.skill_id for j in active):
            raise ValueError("同一 Skill 只能有一个活动任务")
        if len(active) >= 12:
            raise ValueError("任务容量已满：最多 2 个运行任务和 10 个等待任务")
        suite = self.store.get(Suite, options.suite_id or workspace.suite_id)
        if (
            suite.skill_id != workspace.skill_id
            or suite.status != "confirmed"
            or digest(suite.cases) != suite.content_hash
        ):
            raise ValueError("只能使用属于当前 Skill 的完整、已确认评测集")
        version = self.store.get(Version, workspace.version_id)
        if any("scripts/" in p for p in version.files) or "scripts/" in version.content:
            raise ValueError("当前仅支持文本 Skill；需要执行脚本的 Skill 不可评测")
        job = Job(
            skill_id=workspace.skill_id,
            session_id=workspace.id,
            version_id=version.id,
            suite_id=suite.id,
        )
        workspace.job_id = job.id
        self.store.save(job, workspace)
        self.store.emit(
            job.id, "queued", {"message": "任务已入队", "suite_hash": suite.content_hash}
        )
        task = asyncio.create_task(self.worker(job.id, options))
        self.tasks[job.id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job.id, None))
        return job

    async def cancel(self, job_id):
        job = self.store.get(Job, job_id)
        if job.status not in ACTIVE:
            return job
        task = self.tasks.get(job_id)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Queued tasks can be cancelled before their coroutine starts.
        job = self.store.get(Job, job_id)
        if job.status in ACTIVE:
            job.status = "stopped"
            self.store.save(job)
            self.store.emit(job.id, "stopped", {"message": "任务已取消"})
        return job

    async def close(self):
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)

    async def worker(self, job_id, options):
        try:
            async with self.slots:
                job = self.store.get(Job, job_id)
                job.status = "running"
                self.store.save(job)
                self.store.emit(job.id, "running", {"message": "开始训练集优化"})
                await self.execute(job, options)
        except asyncio.CancelledError:
            job = self.store.get(Job, job_id)
            job.status = "stopped"
            self.store.save(job)
            self.store.emit(job_id, "stopped", {"message": "任务已取消，不允许发布"})
            raise
        except Exception as error:
            job = self.store.get(Job, job_id)
            # Do not persist raw provider errors: they may contain credentials.
            job.status, job.error = (
                "failed",
                f"{type(error).__name__}：任务失败，请检查模型配置、预算或输入",
            )
            self.store.save(job)
            self.store.emit(job_id, "failed", {"message": job.error})

    async def execute(self, job, options):
        store = self.store
        origin, suite = store.get(Version, job.version_id), store.get(Suite, job.suite_id)
        skill = store.get(Skill, job.skill_id)
        baseline = (
            store.get(Version, skill.current_version_id) if skill.current_version_id else origin
        )
        agents = self.agents_factory(options.request_budget)
        evaluator = Evaluator(agents)
        train = [c for c in suite.cases if c["split"] == "train"]
        holdout = [c for c in suite.cases if c["split"] == "holdout"]
        versions = {origin.content: origin}
        best = origin

        async def record(version, split, cases):
            calls, tokens = agents.calls, agents.tokens
            report = await evaluator.run(version.content, references(version.files), cases)
            report.update(
                {
                    "suite_hash": suite.content_hash,
                    "version_id": version.id,
                    "model_requests": agents.calls - calls,
                    "tokens": agents.tokens - tokens,
                }
            )
            store.save(
                Run(
                    job_id=job.id,
                    version_id=version.id,
                    suite_id=suite.id,
                    split=split,
                    report=report,
                )
            )
            store.emit(
                job.id,
                "evaluation",
                {
                    "version_id": version.id,
                    "split": split,
                    "score": report["score"],
                    "safety_score": report["safety_score"],
                    "errors": report["errors"],
                },
            )
            return report

        async def evaluate(content, phase, summary=""):
            version = versions.get(content)
            if version is None:
                version = Version(
                    skill_id=skill.id,
                    parent_id=best.id,
                    content=content,
                    files=replace_skill(origin.files, content),
                    summary=summary,
                )
                versions[content] = version
                store.save(version)
            return await record(version, "train", train)

        async def candidate(content, kept):
            nonlocal best
            version = versions[content]
            version.status = "candidate" if kept else "rejected"
            store.save(version)
            if kept:
                best = version

        def emit(name, data):
            store.emit(job.id, name, data)

        baseline_train = (
            await record(baseline, "train", train) if baseline.id != origin.id else None
        )
        _, initial_train, final_train, history = await SkillOptimizer(agents).optimize(
            origin.files, train, options.max_rounds, evaluate, candidate, emit
        )
        baseline_train = baseline_train or initial_train
        emit(
            "validation_start",
            {"message": "优化结束，开始基线/候选保留集各 3 次配对复测；不回传给优化 Agent"},
        )
        before, after = [], []
        for _ in range(3):
            before.append(await record(baseline, "holdout", holdout))
            after.append(await record(best, "holdout", holdout))
        gate = release_gate(baseline_train, final_train, before, after)
        # An existing version must never lose its historical release status.
        if best.id != origin.id or best.status not in {"approved", "released"}:
            best.status = gate["decision"]
        result = {
            **gate,
            "candidate_version_id": best.id,
            "baseline_version_id": baseline.id,
            "published_at_start": skill.current_version_id,
            "train_before": baseline_train["score"],
            "train_after": final_train["score"],
            "experiments": history,
            "model_requests": agents.calls,
            "tokens": agents.tokens,
            "mode": agents.mode,
            "model": agents.model,
            "suite_hash": suite.content_hash,
            "demo_notice": "离线分数为脚本模拟，不是模型提升" if agents.mode == "demo" else None,
        }
        job.status, job.result = "completed", result
        store.save(
            best,
            job,
            Decision(
                skill_id=skill.id,
                version_id=best.id,
                job_id=job.id,
                action=gate["decision"],
                reason="；".join(gate["reasons"]),
            ),
        )
        emit("completed", result)
