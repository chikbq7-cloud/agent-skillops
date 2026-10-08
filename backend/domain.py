"""Validated inputs shared by the API, evaluator and optimizer."""

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Literal

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class Criterion(BaseModel):
    id: str
    name: str = ""
    kind: Literal["llm", "keyword", "forbidden", "json_schema"] = "llm"
    question: str = ""
    pass_condition: str = ""
    fail_condition: str = ""
    values: list[str] = Field(default_factory=list)
    schema_: dict = Field(default_factory=dict, alias="schema")
    safety: StrictBool = False
    critical: StrictBool = False

    @model_validator(mode="after")
    def check_rule(self):
        if not self.id.strip():
            raise ValueError("评测标准必须有 ID")
        if self.kind == "llm" and not self.question.strip():
            raise ValueError("语义评测必须填写问题")
        if self.kind in {"keyword", "forbidden"} and (
            not self.values or any(not v for v in self.values)
        ):
            raise ValueError("规则评测必须填写非空关键词")
        if self.kind == "json_schema":
            if not self.schema_:
                raise ValueError("JSON 格式评测必须填写 schema")
            try:
                Draft202012Validator.check_schema(self.schema_)
            except SchemaError as error:
                raise ValueError("无效 JSON schema") from error
        return self


class Case(BaseModel):
    id: str
    name: str = ""
    input: str = Field(min_length=1)
    split: Literal["train", "holdout"] = "train"
    checks: list[Criterion] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_checks(self):
        if not self.id.strip() or not self.input.strip():
            raise ValueError("场景 ID 与输入不能为空")
        ids = [c.id for c in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("同一场景中的标准 ID 不能重复")
        return self


class SuiteInput(BaseModel):
    cases: list[Case] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_cases(self):
        ids = [c.id for c in self.cases]
        inputs = [c.input.strip() for c in self.cases]
        if len(ids) != len(set(ids)) or len(inputs) != len(set(inputs)):
            raise ValueError("场景 ID 与输入不能重复，训练集和保留集不得重叠")
        return self

    def confirm(self):
        train = [c for c in self.cases if c.split == "train"]
        holdout = [c for c in self.cases if c.split == "holdout"]
        if len(train) < 5 or len(holdout) < 3:
            raise ValueError("确认评测集至少需要 5 个训练场景和 3 个保留场景")
        if not all(any(k.safety for c in group for k in c.checks) for group in (train, holdout)):
            raise ValueError("训练集和保留集都需要至少一项安全检查")


class Judgment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    eval_id: str
    passed: StrictBool
    reason: str


class Judgments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[Judgment]


class StartInput(BaseModel):
    suite_id: str | None = None
    max_rounds: int = Field(default=5, ge=1, le=50)
    request_budget: int = Field(default=150, ge=1, le=1000)


def skill_content(files: dict[str, str]) -> tuple[str, dict]:
    paths = [p for p in files if PurePosixPath(p).name == "SKILL.md"]
    if len(paths) != 1:
        raise ValueError("上传内容必须包含且仅包含一个 SKILL.md")
    content = files[paths[0]]
    parts = content.split("---", 2)
    if not content.startswith("---") or len(parts) < 3:
        raise ValueError("SKILL.md 必须包含 YAML frontmatter")
    try:
        metadata = yaml.safe_load(parts[1])
    except yaml.YAMLError as error:
        raise ValueError("YAML 元数据格式错误") from error
    if not isinstance(metadata, dict) or not all(
        isinstance(metadata.get(k), str) and metadata[k].strip() for k in ("name", "description")
    ):
        raise ValueError("Skill 元数据必须包含 name 和 description")
    if not parts[2].strip():
        raise ValueError("Skill 正文不能为空")
    references = [p.rstrip(".") for p in re.findall(r"(?:references|scripts)/[\w./-]+", content)]
    base = str(PurePosixPath(paths[0]).parent)
    for path in references:
        if path not in files and str(PurePosixPath(base) / path) not in files:
            raise ValueError(f"引用文件不存在：{path}")
    return content, metadata


def replace_skill(files: dict[str, str], content: str) -> dict[str, str]:
    return {p: content if PurePosixPath(p).name == "SKILL.md" else v for p, v in files.items()}


def references(files: dict[str, str]) -> str:
    return "\n\n".join(f"## {p}\n{v}" for p, v in files.items() if "references/" in p)
