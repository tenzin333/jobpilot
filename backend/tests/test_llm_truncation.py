"""Exercise truncation through the real tailoring and structured-client paths."""
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.llm import client
from app.llm.tailoring import TailoredResume, tailor
from app.models import Job, Profile
from app.resume import parse as resume_parse


@pytest.fixture
def local_llm(monkeypatch):
    monkeypatch.setattr(client, "get_settings", lambda: Settings(
        _env_file=None, quality_backend="openai",
        openai_base_url="http://llm.test/v1", openai_quality_model="fixture",
    ))


def completion(content, finish_reason="stop"):
    return httpx.Response(200, json={"choices": [{
        "message": {"content": content}, "finish_reason": finish_reason,
    }]})


def test_tailoring_has_room_for_resume_and_cover_letter(local_llm, monkeypatch):
    complete = TailoredResume(
        summary="Python engineer", highlighted_skills=["Python"],
        cover_letter="I build Python services. " * 100,
    ).model_dump_json()

    def post(url, *, json, **kwargs):
        if json["max_tokens"] < 2000:
            return completion(complete[:1100], "length")
        return completion(complete)

    monkeypatch.setattr(httpx, "post", post)
    result, removed = tailor(
        Job(company="Fixture", title="Engineer", description="Python services"),
        Profile(full_name="Fixture", email="fixture@example.com", skills=["Python"]),
    )
    assert result.cover_letter == json.loads(complete)["cover_letter"]
    assert removed == []


def test_length_finish_retries_with_more_room_even_if_json_is_valid(local_llm, monkeypatch):
    budgets = []

    def post(url, *, json, **kwargs):
        budgets.append(json["max_tokens"])
        if len(budgets) == 1:
            return completion('{"summary":"partial"}', "length")
        return completion('{"summary":"complete"}')

    monkeypatch.setattr(httpx, "post", post)
    result = client.parse_structured(system="test", user="test", schema=TailoredResume, max_tokens=256)
    assert result.summary == "complete"
    assert budgets == [256, 512]


def test_persistent_truncation_stops_with_clear_error(local_llm, monkeypatch):
    budgets = []

    def post(url, *, json, **kwargs):
        budgets.append(json["max_tokens"])
        return completion('{"summary":"cut off', "length")

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(client.LLMError, match="truncated"):
        client.parse_structured(system="test", user="test", schema=TailoredResume, max_tokens=256)
    assert budgets == [256, 512]


def test_malformed_json_retry_keeps_budget(local_llm, monkeypatch):
    budgets = []

    def post(url, *, json, **kwargs):
        budgets.append(json["max_tokens"])
        return completion('invalid' if len(budgets) == 1 else '{"summary":"fixed"}')

    monkeypatch.setattr(httpx, "post", post)
    result = client.parse_structured(system="test", user="test", schema=TailoredResume, max_tokens=256)
    assert result.summary == "fixed"
    assert budgets == [256, 256]


def test_hf_truncation_also_retries_with_more_room(monkeypatch):
    import huggingface_hub

    monkeypatch.setattr(client, "get_settings", lambda: Settings(
        _env_file=None, quality_backend="hf", hf_token="test-token",
    ))
    budgets = []

    def chat_completion(**kwargs):
        budgets.append(kwargs["max_tokens"])
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="length" if len(budgets) == 1 else "stop",
            message=SimpleNamespace(content='{"summary":"complete"}'),
        )])

    monkeypatch.setattr(huggingface_hub, "InferenceClient", lambda **kwargs: SimpleNamespace(
        chat_completion=chat_completion,
    ))
    result = client.parse_structured(system="test", user="test", schema=TailoredResume, max_tokens=256)
    assert result.summary == "complete"
    assert budgets == [256, 512]


def test_resume_extraction_has_room_for_a_multi_section_profile(local_llm, monkeypatch):
    complete = resume_parse.ResumeExtraction(
        full_name="Tenzin Candidate",
        email="candidate@example.com",
        location="London",
        linkedin="https://linkedin.com/in/candidate",
        skills=["Python", "FastAPI", "PostgreSQL", "React"],
        experience=[resume_parse.ExperienceItem(
            company="Example Ltd",
            title="Software Engineer",
            start="2022",
            end="Present",
            bullets=[
                "Built reliable application services.",
                "Improved deployment workflows.",
            ],
        )],
        education=[resume_parse.EducationItem(
            institution="Example University", degree="BSc Computer Science", year="2022",
        )],
    ).model_dump_json()
    budgets = []

    def post(url, *, json, **kwargs):
        budgets.append(json["max_tokens"])
        if json["max_tokens"] < 1024:
            return completion(complete[:400], "length")
        return completion(complete)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(resume_parse, "extract_text", lambda _path: "multi-section resume text")

    fields = resume_parse.parse_resume(Path("candidate.pdf"))

    assert fields["full_name"] == "Tenzin Candidate"
    assert fields["skills"] == ["Python", "FastAPI", "PostgreSQL", "React"]
    assert fields["experience"][0]["company"] == "Example Ltd"
    assert budgets == [4096]
