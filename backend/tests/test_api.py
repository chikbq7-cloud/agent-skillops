import io
import json
import time
import zipfile

import pytest
from adk_optimizer import Agents
from app import create_app
from examples import example
from fastapi.testclient import TestClient
from store import Job, Skill


def load(client, kind="code-review"):
    response = client.post(f"/api/examples/{kind}")
    assert response.status_code == 200
    workspace = response.json()
    assert client.post(f"/api/suites/{workspace['suite']['id']}/confirm").status_code == 200
    return workspace


def wait(client, job_id):
    for _ in range(1000):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in {"queued", "running", "stopping"}:
            return job
        time.sleep(0.01)
    raise AssertionError("任务没有终止")


def start(client, workspace, **options):
    response = client.post(f"/api/sessions/{workspace['session_id']}/start", json=options)
    assert response.status_code == 200, response.text
    return response.json()


def test_full_flow_frozen_suite_publish_download_and_sse(client):
    ws = load(client)
    old = client.get(f"/api/suites/{ws['suite']['id']}").json()
    newer = client.post(
        f"/api/sessions/{ws['session_id']}/suites", json={"cases": old["cases"]}
    ).json()
    assert newer["id"] != old["id"] and newer["number"] == 2
    assert client.get(f"/api/suites/{old['id']}").json() == old
    assert client.post(f"/api/sessions/{ws['session_id']}/start", json={}).status_code == 400
    job = wait(client, start(client, ws, suite_id=old["id"])["id"])
    assert job["result"]["decision"] == "approved"
    assert job["result"]["model_requests"] == 58
    assert len([r for r in job["runs"] if r["split"] == "holdout"]) == 6
    skill = client.get(f"/api/skills/{ws['skill_id']}").json()
    assert skill["skill"]["current_version_id"] is None  # no automatic release
    version_id = job["result"]["candidate_version_id"]
    assert client.post(f"/api/skills/{ws['skill_id']}/publish/{version_id}").status_code == 200
    download = client.get(f"/api/versions/{version_id}/download")
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert "[validated-fix]" in archive.read("SKILL.md").decode()
    events = client.get(f"/api/jobs/{job['id']}/events").text
    last = job["events"][-1]["id"]
    assert "completed" in events and f"id: {last}" in events
    assert (
        client.get(f"/api/jobs/{job['id']}/events", headers={"Last-Event-ID": str(last)}).text == ""
    )


def test_unsafe_candidate_is_saved_but_never_published(client):
    ws = load(client, "unsafe")
    job = wait(client, start(client, ws)["id"])
    assert job["result"]["decision"] == "rejected"
    assert all(not e["kept"] for e in job["result"]["experiments"])
    assert any(r["report"]["safety_score"] == 0 for r in job["runs"])
    versions = client.get(f"/api/skills/{ws['skill_id']}").json()["versions"]
    assert any("[validated-fix]" in v["content"] and v["status"] == "rejected" for v in versions)
    for v in versions:
        assert client.post(f"/api/skills/{ws['skill_id']}/publish/{v['id']}").status_code == 400


def test_budget_stop_and_duplicate_skill_job(client, monkeypatch):
    monkeypatch.setenv("SKILLOPS_DEMO_DELAY", ".1")
    ws = load(client)
    job = start(client, ws)
    assert client.post(f"/api/sessions/{ws['session_id']}/start", json={}).status_code == 400
    assert client.post(f"/api/jobs/{job['id']}/stop").json()["status"] == "stopped"
    time.sleep(0.1)
    assert client.get(f"/api/jobs/{job['id']}").json()["result"] is None
    monkeypatch.setenv("SKILLOPS_DEMO_DELAY", ".001")
    failed = wait(client, start(client, ws, request_budget=1)["id"])
    assert failed["status"] == "failed" and failed["result"] is None
    assert "BudgetExceeded" in failed["error"]


def test_same_name_is_not_same_skill_and_suite_ownership(client):
    one, two = load(client), load(client)
    assert one["skill_id"] != two["skill_id"]
    assert (
        client.post(
            f"/api/sessions/{one['session_id']}/start", json={"suite_id": two["suite"]["id"]}
        ).status_code
        == 400
    )
    assert (
        client.post(f"/api/skills/{one['skill_id']}/rollback/{two['version_id']}").status_code
        == 404
    )


@pytest.mark.parametrize(
    "name,content",
    [
        ("../SKILL.md", "bad"),
        (".env", "KEY=value"),
        ("SKILL.md", "no metadata"),
        ("image.png", "bad"),
    ],
)
def test_unsafe_uploads_rejected(client, name, content):
    assert client.post("/api/upload", files={"files": (name, content.encode())}).status_code == 400


def test_zip_traversal_and_script_skill(client):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("../SKILL.md", example()["files"]["SKILL.md"])
    assert (
        client.post("/api/upload", files={"files": ("skill.zip", data.getvalue())}).status_code
        == 400
    )
    content = example()["files"]["SKILL.md"] + "\n执行 scripts/main.py"
    ws = client.post(
        "/api/upload",
        files=[
            ("files", ("SKILL.md", content.encode())),
            ("files", ("scripts/main.py", b"print(1)")),
        ],
    ).json()
    suite = client.post(f"/api/sessions/{ws['session_id']}/suites", json=example()["suite"]).json()
    client.post(f"/api/suites/{suite['id']}/confirm")
    assert client.post(f"/api/sessions/{ws['session_id']}/start", json={}).status_code == 400


def test_restart_preserves_data_and_marks_old_tasks_interrupted(tmp_path):
    path = str(tmp_path / "persistent.db")
    with TestClient(create_app(path)) as client:
        ws = load(client)
        store = client.app.state.store
        job = Job(
            skill_id=ws["skill_id"],
            session_id=ws["session_id"],
            version_id=ws["version_id"],
            suite_id=ws["suite"]["id"],
            status="running",
        )
        store.save(job)
    with TestClient(create_app(path)) as client:
        assert client.get(f"/api/sessions/{ws['session_id']}").status_code == 200
        assert client.get(f"/api/jobs/{job.id}").json()["status"] == "interrupted"
        assert client.get(f"/api/suites/{ws['suite']['id']}").json()["status"] == "confirmed"


def test_holdout_never_reaches_analyst_or_mutator(tmp_path):
    prompts = []

    class Tracking(Agents):
        async def ask(self, role, prompt):
            prompts.append((role, prompt))
            return await super().ask(role, prompt)

    with TestClient(create_app(str(tmp_path / "test.db"), Tracking)) as client:
        ws = load(client)
        wait(client, start(client, ws)["id"])
        holdout = [c["input"] for c in ws["suite"]["cases"] if c["split"] == "holdout"]
        optimization = [p for role, p in prompts if role in {"analyst", "mutator"}]
        assert optimization and all(value not in p for p in optimization for value in holdout)
        assert any("failed_training_checks" in p and '"output"' in p for p in optimization)


def test_running_and_waiting_limits(client, monkeypatch):
    monkeypatch.setenv("SKILLOPS_DEMO_DELAY", ".5")
    ids = [start(client, load(client))["id"] for _ in range(12)]
    states = [client.get(f"/api/jobs/{id_}").json()["status"] for id_ in ids]
    assert states.count("running") == 2 and states.count("queued") == 10
    extra = load(client)
    assert client.post(f"/api/sessions/{extra['session_id']}/start", json={}).status_code == 400
    for id_ in ids:
        client.post(f"/api/jobs/{id_}/stop")


def test_rollback_requires_previously_released_version(client):
    ws = load(client)
    job = wait(client, start(client, ws)["id"])
    candidate = job["result"]["candidate_version_id"]
    assert client.post(f"/api/skills/{ws['skill_id']}/rollback/{candidate}").status_code == 400
    assert client.post(f"/api/skills/{ws['skill_id']}/publish/{candidate}").status_code == 200
    assert client.post(f"/api/skills/{ws['skill_id']}/rollback/{candidate}").status_code == 200
    assert (
        client.post(f"/api/skills/{ws['skill_id']}/rollback/{ws['version_id']}").status_code == 400
    )


def test_stale_approval_cannot_publish_after_pointer_changes(client):
    ws = load(client)
    job = wait(client, start(client, ws)["id"])
    store = client.app.state.store
    skill = store.get(Skill, ws["skill_id"])
    skill.current_version_id = ws["version_id"]
    store.save(skill)
    assert (
        client.post(
            f"/api/skills/{ws['skill_id']}/publish/{job['result']['candidate_version_id']}"
        ).status_code
        == 400
    )


def test_real_mode_needs_server_key_and_never_returns_it(client, monkeypatch):
    ws = load(client)
    monkeypatch.setenv("SKILLOPS_MODE", "gemini")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert client.post(f"/api/sessions/{ws['session_id']}/start", json={}).status_code == 400
    monkeypatch.setenv("GOOGLE_API_KEY", "FAKE_SECRET_FOR_TEST_ONLY")
    settings = client.get("/api/settings")
    assert settings.json()["key_configured"] is True
    assert "FAKE_SECRET_FOR_TEST_ONLY" not in settings.text


def test_version_upload_preserves_skill_and_checks_metadata(client):
    ws = load(client)
    content = example()["files"]["SKILL.md"] + "\n新增约束。"
    uploaded = client.post(
        "/api/upload",
        data={"skill_id": ws["skill_id"]},
        files={"files": ("SKILL.md", content.encode())},
    )
    assert uploaded.status_code == 200 and uploaded.json()["skill_id"] == ws["skill_id"]
    assert uploaded.json()["version_id"] != ws["version_id"]
    changed_name = content.replace("name: code-reviewer", "name: different")
    assert (
        client.post(
            "/api/upload",
            data={"skill_id": ws["skill_id"]},
            files={"files": ("SKILL.md", changed_name.encode())},
        ).status_code
        == 400
    )


def test_saved_candidates_are_rejected_on_metadata_mutation(tmp_path):
    class BadMutator(Agents):
        def demo(self, role, prompt):
            if role == "mutator":
                data = json.loads(super().demo(role, prompt))
                data["new_skill_md"] = data["new_skill_md"].replace(
                    "name: code-reviewer", "name: rewritten"
                )
                return json.dumps(data)
            return super().demo(role, prompt)

    with TestClient(create_app(str(tmp_path / "test.db"), BadMutator)) as client:
        ws = load(client)
        job = wait(client, start(client, ws)["id"])
        assert job["result"]["decision"] == "rejected"
        assert len(job["result"]["experiments"]) == 2
        assert all("元数据" in e["reason"] for e in job["result"]["experiments"])
        assert len(client.get(f"/api/skills/{ws['skill_id']}").json()["versions"]) == 1


def test_two_releases_and_pointer_rollback(tmp_path):
    class Upgrade(Agents):
        def demo(self, role, prompt):
            response = super().demo(role, prompt)
            if role == "analyst" and '"upgrade"' in prompt:
                data = json.loads(response)
                data["change_description"] = "upgrade"
                return json.dumps(data)
            if role == "mutator" and '"upgrade"' in prompt:
                data = json.loads(response)
                data["new_skill_md"] += "\n<!-- upgrade-ready -->\n输出应包含升级说明。"
                return json.dumps(data)
            if role == "executor" and "upgrade-ready" in prompt:
                return response + "升级"
            return response

    with TestClient(create_app(str(tmp_path / "test.db"), Upgrade)) as client:
        first = load(client)
        first_job = wait(client, start(client, first)["id"])
        v1 = first_job["result"]["candidate_version_id"]
        assert client.post(f"/api/skills/{first['skill_id']}/publish/{v1}").status_code == 200
        fresh = client.post(
            "/api/upload",
            data={"skill_id": first["skill_id"]},
            files={"files": ("SKILL.md", example()["files"]["SKILL.md"].encode())},
        ).json()
        suite = example()["suite"]
        for case in suite["cases"]:
            case["checks"].append({"id": "upgrade", "kind": "keyword", "values": ["升级"]})
        saved = client.post(f"/api/sessions/{fresh['session_id']}/suites", json=suite).json()
        assert client.post(f"/api/suites/{saved['id']}/confirm").status_code == 200
        second_job = wait(client, start(client, fresh)["id"])
        assert second_job["result"]["baseline_version_id"] == v1
        assert second_job["result"]["decision"] == "approved"
        v2 = second_job["result"]["candidate_version_id"]
        assert v1 != v2
        assert client.post(f"/api/skills/{first['skill_id']}/publish/{v2}").status_code == 200
        assert client.post(f"/api/skills/{first['skill_id']}/rollback/{v1}").status_code == 200
        snapshot = client.get(f"/api/skills/{first['skill_id']}").json()
        assert snapshot["skill"]["current_version_id"] == v1
        assert (
            next(d for d in snapshot["decisions"] if d["action"] == "rollback")["previous_id"] == v2
        )
