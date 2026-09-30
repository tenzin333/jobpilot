from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

from app.candidate_brain.models import Basic, Candidate, Education, Employment, Skills
from app.candidate_brain.services import ProfileReview
from app.models import CandidateProfileReview, Profile
from app.db import ensure_schema


def _candidate(name: str, company: str, skill: str) -> Candidate:
    return Candidate(
        profile=Basic(
            full_name=name,
            email=f"{name.split()[0].lower()}@example.com",
            location="Pune",
            professional_title="Platform Engineer",
        ),
        employment=[Employment(
            company=company,
            designation="Platform Engineer",
            start_date_raw="2022",
            responsibilities=["Built reliable deployment tooling"],
        )],
        education=[Education(institution="Example University", degree="BSc", year="2021")],
        skills=Skills(programming_languages=[skill]),
    )


def test_pending_profile_does_not_replace_approved_profile_until_approval(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        review = ProfileReview(session)
        first = _candidate("Alex Dev", "First Co", "Python")
        review.stage(
            first,
            resume_path=str(tmp_path / "first.pdf"),
            raw_text="Alex Dev\nFirst Co\nPython",
            answer_bank={"linkedin": "https://example.com/alex"},
        )
        review.approve()

        profile = session.exec(select(Profile)).one()
        assert profile.full_name == "Alex Dev"
        assert profile.skills == ["Python"]
        assert profile.experience[0]["company"] == "First Co"

        second = _candidate("Alex Dev", "Second Co", "Go")
        row = review.stage(
            second,
            resume_path=str(tmp_path / "second.pdf"),
            raw_text="Alex Dev\nSecond Co\nGo",
        )
        assert row.status == "pending"
        session.refresh(profile)
        assert profile.skills == ["Python"]
        assert profile.experience[0]["company"] == "First Co"

        review.reject()
        row = review.get()
        assert row is not None and row.status == "approved"
        assert Candidate.model_validate(row.approved_profile).employment[0].company == "First Co"
        session.refresh(profile)
        assert profile.base_resume_path.endswith("first.pdf")


def test_edited_draft_is_the_profile_promoted_to_application_workflows(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        review = ProfileReview(session)
        generated = _candidate("Sam Candidate", "Generated Co", "Python")
        review.stage(
            generated,
            resume_path=str(tmp_path / "candidate.pdf"),
            raw_text="source resume evidence",
            answer_bank={"current_location": "Pune"},
        )
        corrected = generated.model_copy(deep=True)
        corrected.employment[0].company = "Correct Company"
        corrected.skills.programming_languages = ["Python", "TypeScript"]
        review.update(corrected)
        approved = review.approve()

        assert approved.employment[0].company == "Correct Company"
        profile = session.exec(select(Profile)).one()
        assert profile.skills == ["Python", "TypeScript"]
        assert profile.experience[0]["company"] == "Correct Company"
        assert profile.education == [{"institution": "Example University", "degree": "BSc", "year": "2021"}]
        assert profile.raw_text == "source resume evidence"


def test_startup_upgrades_the_original_candidate_review_table():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("""CREATE TABLE candidate_profile_review (
            id INTEGER PRIMARY KEY,
            status VARCHAR NOT NULL,
            pending_profile JSON NOT NULL,
            approved_profile JSON NOT NULL,
            pending_resume_path VARCHAR NOT NULL,
            pending_raw_text VARCHAR NOT NULL,
            pending_answer_bank JSON NOT NULL,
            source_filename VARCHAR NOT NULL,
            generated_at DATETIME NOT NULL,
            approved_at DATETIME,
            updated_at DATETIME NOT NULL
        )"""))
        connection.execute(text("""INSERT INTO candidate_profile_review VALUES (
            1, 'pending', '{}', '{}', '', '', '{}', '', CURRENT_TIMESTAMP, NULL, CURRENT_TIMESTAMP
        )"""))

    ensure_schema(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("candidate_profile_review")}
    assert {
        "approved_resume_path", "approved_raw_text", "approved_answer_bank",
        "approved_source_filename",
    } <= columns
    with Session(engine) as session:
        row = session.exec(select(CandidateProfileReview)).one()
    assert row.approved_resume_path == ""
    assert row.approved_raw_text == ""
    assert row.approved_answer_bank == {}
    assert row.approved_source_filename == ""
