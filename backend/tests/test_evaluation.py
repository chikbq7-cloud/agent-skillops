import asyncio
import json

import pytest
from adk_optimizer import Agents, BudgetExceeded
from domain import Case, Criterion, SuiteInput, skill_content
from evaluation import Evaluator, release_gate, rule_check, summarize
from examples import example
from pydantic import ValidationError


def report(score=50, safety=100, critical=True, errors=0):
    return {
        "score": score,
        "safety_score": safety,
        "safety_checks": 1,
        "errors": errors,
        "details": [
            {
                "scenario_id": "c",
                "eval_id": "e",
                "critical": True,
                "status": "passed" if critical else "failed",
            }
        ],
    }


def test_release_gate_requires_improvement_and_three_repeats():
    before, after = report(), report(80)
    assert release_gate(before, after, [before] * 3, [after] * 3)["decision"] == "approved"
    assert release_gate(before, before, [before] * 3, [before] * 3)["decision"] == "rejected"
    assert "保留集复测未完成" in release_gate(before, after, [before], [after])["reasons"]


@pytest.mark.parametrize(
    "bad,reason",
    [
        (report(90, 0), "候选安全门槛未通过"),
        (report(90, critical=False), "关键能力发生回归"),
        (report(90, errors=1), "评测存在错误"),
        (report(10), "保留集平均分下降"),
    ],
)
def test_release_rejects_bad_holdout(bad, reason):
    assert reason in release_gate(report(), report(90), [report()] * 3, [bad] * 3)["reasons"]


class Responses(Agents):
    def __init__(self, values):
        super().__init__()
        self.values, self.prompts = iter(values), []

    async def ask(self, role, prompt):
        self.prompts.append((role, prompt))
        return next(self.values)


@pytest.mark.parametrize(
    "results",
    [
        [],
        [{"eval_id": "unknown", "passed": True, "reason": "x"}],
        [{"eval_id": "meaning", "passed": "true", "reason": "x"}],
        [{"eval_id": "meaning", "passed": True, "reason": "x"}] * 2,
    ],
)
def test_judge_missing_unknown_string_bool_duplicate_are_errors(results):
    case = Case.model_validate(example()["suite"]["cases"][0])
    response = json.dumps({"results": results})
    agents = Responses(["完成 建议", response, response])
    scored = asyncio.run(Evaluator(agents).run("skill", "", [case.model_dump(by_alias=True)]))
    assert scored["errors"] == 4
    assert scored["total"] == 4 and scored["passed"] == 0
    assert len(agents.prompts) == 3


def test_judge_repairs_once_and_keeps_full_evidence():
    case = example()["suite"]["cases"][0]
    good = json.dumps({"results": [{"eval_id": "meaning", "passed": True, "reason": "具体证据"}]})
    scored = asyncio.run(
        Evaluator(Responses(["完成 建议", "not-json", good])).run(
            "actual skill", "reference data", [case]
        )
    )
    assert scored["score"] == 100 and scored["errors"] == 0
    item = scored["details"][2]
    assert (
        item["reason"] == "具体证据"
        and item["input"] == case["input"]
        and item["output"] == "完成 建议"
    )
    assert item["criterion"]["question"]


def test_rules_and_expected_denominator():
    assert rule_check("合格且具体", Criterion(id="e", kind="keyword", values=["合格", "具体"]))
    assert not rule_check(
        "DEMO_SECRET", Criterion(id="e", kind="forbidden", values=["DEMO_SECRET"])
    )
    check = Criterion.model_validate(
        {
            "id": "j",
            "kind": "json_schema",
            "schema": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
        }
    )
    assert rule_check('{"name":"test"}', check)
    assert not rule_check('{"name":4}', check) and not rule_check("not json", check)
    with pytest.raises(ValidationError):
        Criterion.model_validate(
            {"id": "bad", "kind": "json_schema", "schema": {"type": "unknown"}}
        )
    scored = summarize(
        [{"eval_id": "e", "safety": False, "status": s} for s in ["passed", "failed", "error"]]
    )
    assert scored["score"] == 33.3 and scored["total"] == 3


def test_suite_confirmation_and_overlap():
    data = example()["suite"]
    SuiteInput.model_validate(data).confirm()
    data["cases"][-1]["input"] = data["cases"][0]["input"]
    with pytest.raises(ValidationError):
        SuiteInput.model_validate(data)
    data = example()["suite"]
    data["cases"] = data["cases"][:5]
    with pytest.raises(ValueError, match="至少"):
        SuiteInput.model_validate(data).confirm()
    data = example()["suite"]
    for c in data["cases"]:
        c["checks"] = [k for k in c["checks"] if not k.get("safety")]
    with pytest.raises(ValueError, match="安全"):
        SuiteInput.model_validate(data).confirm()


def test_skill_requires_valid_metadata_and_references():
    content = example()["files"]["SKILL.md"]
    with pytest.raises(ValueError, match="引用文件"):
        skill_content({"SKILL.md": content + "\n参考 references/rules.md"})
    skill_content(
        {"SKILL.md": content + "\n参考 references/rules.md。", "references/rules.md": "rules"}
    )
    with pytest.raises(ValueError):
        skill_content({"SKILL.md": "---\nname: [bad\n---\nbody"})


def test_budget_counts_every_request():
    async def scenario():
        agents = Agents(budget=1)
        await agents.ask("executor", "test")
        with pytest.raises(BudgetExceeded):
            await agents.ask("executor", "test")
        assert agents.calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("code,expected", [(429, 3), (503, 3), (401, 1)])
def test_only_transient_errors_retry_and_attempts_count(monkeypatch, code, expected):
    class ProviderError(Exception):
        def __init__(self):
            self.code = code

    class Provider(Agents):
        async def real(self, role, prompt):
            raise ProviderError()

    async def no_wait(_):
        pass

    monkeypatch.setattr("adk_optimizer.asyncio.sleep", no_wait)
    agents = Provider(mode="gemini")
    with pytest.raises(ProviderError):
        asyncio.run(agents.ask("executor", "input"))
    assert agents.calls == expected


def test_request_includes_skill_references_and_input():
    case = example()["suite"]["cases"][0]
    judge = json.dumps({"results": [{"eval_id": "meaning", "passed": True, "reason": "x"}]})
    agents = Responses(["完成 建议", judge])
    asyncio.run(Evaluator(agents).run("the exact skill", "reference text", [case]))
    executor_prompt = agents.prompts[0][1]
    assert all(
        value in executor_prompt for value in ["the exact skill", "reference text", case["input"]]
    )
