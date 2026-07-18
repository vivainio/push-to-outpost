from dataclasses import dataclass, field


@dataclass
class TranscriptMessage:
    role: str
    text: str


@dataclass
class Transcript:
    """Provider-neutral representation of an agent session."""

    session_id: str
    title: str
    cwd: str | None = None
    git_branch: str | None = None
    messages: list[TranscriptMessage] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
