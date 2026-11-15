"""Load and validate the immutable prompt bundle for this application process."""
from functools import lru_cache
from pathlib import Path


PROMPTS = Path(__file__).resolve().parent
PROMPT_FILES = (
    "chat.router.txt",
    "chat.relevance.txt",
    "chat.answer.txt",
    "research.name.txt",
    "research.compare.txt",
    "research.answer.txt",
    "vision.production.txt",
    "vision.recovery.txt",
)


@lru_cache(maxsize=len(PROMPT_FILES))
def load_prompt(filename: str) -> str:
    if filename not in PROMPT_FILES:
        raise RuntimeError(f"Unknown application prompt: {filename}")
    path = PROMPTS / filename
    try:
        prompt = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"Cannot load required prompt: {path}") from exc
    prompt = prompt.strip()
    if not prompt:
        raise RuntimeError(f"Required prompt is empty: {path}")
    return prompt


def validate_prompts() -> None:
    """Fail at startup if any required prompt is missing, unreadable or empty."""
    for filename in PROMPT_FILES:
        load_prompt(filename)
