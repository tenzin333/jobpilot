from datetime import date
from pydantic import BaseModel, Field


class Basic(BaseModel):
    full_name: str
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    professional_title: str | None = None
    professional_summary: str | None = None


class Achievement(BaseModel):
    description: str
    metric: str | None = None
    # Example: "Reduced API response time"
    # Metric: "30%" (only if verified)


class Project(BaseModel):
    name: str
    description: str

    technologies: list[str] = Field(
        default_factory=list
    )

    contributions: list[str] = Field(
        default_factory=list
    )

    challenges: list[str] = Field(
        default_factory=list
    )

    achievements: list[Achievement] = Field(
        default_factory=list
    )

    repository_url: str | None = None


class Employment(BaseModel):
    company: str
    designation: str

    start_date_raw: str | None = None
    end_date_raw: str | None = None

    start_date: date | None = None
    end_date: date | None = None

    responsibilities: list[str] = Field(
        default_factory=list
    )

    technologies: list[str] = Field(
        default_factory=list
    )

    achievements: list[Achievement] = Field(
        default_factory=list
    )


class Education(BaseModel):
    institution: str
    degree: str = ""
    year: str = ""


class Hobbies(BaseModel):
    interests: list[str] = Field(
        default_factory=list
    )

    activities: list[str] = Field(
        default_factory=list
    )


class Skills(BaseModel):
    programming_languages: list[str] = Field(
        default_factory=list
    )

    frameworks: list[str] = Field(
        default_factory=list
    )

    databases: list[str] = Field(
        default_factory=list
    )

    tools: list[str] = Field(
        default_factory=list
    )

    ai_ml: list[str] = Field(
        default_factory=list
    )


class Candidate(BaseModel):
    profile: Basic

    projects: list[Project] = Field(
        default_factory=list
    )

    employment: list[Employment] = Field(
        default_factory=list
    )

    education: list[Education] = Field(
        default_factory=list
    )

    hobbies: Hobbies = Field(
        default_factory=Hobbies
    )

    skills: Skills = Field(
        default_factory=Skills
    )
