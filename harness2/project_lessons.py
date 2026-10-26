"""Reviewed project lessons, separate from experimentally promoted experience."""

from __future__ import annotations

import json
import re
from importlib.resources import files


def load_project_lessons() -> list[dict]:
    """Load the packaged, version-controlled catalog; invalid records fail loudly."""
    lessons = []
    seen = set()
    for resource in sorted(files("harness2").joinpath("lessons").iterdir(),
                           key=lambda item: item.name):
        if not resource.name.endswith(".json"):
            continue
        lesson = json.loads(resource.read_text(encoding="utf-8"))
        required = ("id", "cue", "lesson", "evidence", "confidence")
        if (not isinstance(lesson, dict)
                or any(not isinstance(lesson.get(key), str) or not lesson[key].strip()
                       for key in required)
                or lesson.get("status") != "verified_project_lesson"):
            raise ValueError(f"Invalid project lesson: {resource.name}")
        groups = lesson.get("match_all")
        if (not isinstance(groups, list) or not groups
                or any(not isinstance(group, list) or not group
                       or any(not isinstance(term, str) or not _words(term)
                              for term in group) for group in groups)):
            raise ValueError(f"Invalid project lesson cues: {resource.name}")
        if lesson["id"] in seen:
            raise ValueError(f"Duplicate project lesson: {lesson['id']}")
        seen.add(lesson["id"])
        lessons.append(lesson)
    return lessons


def _words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def relevant_project_lessons(task_prompt: str, lessons: list[dict]) -> list[dict]:
    """Require one whole phrase from every cue group in the public task text."""
    text = f" {_words(task_prompt)} "
    return [lesson for lesson in lessons if all(
        any(f" {_words(term)} " in text for term in group)
        for group in lesson["match_all"]
    )]


def lesson_context(lessons: list[dict]) -> str:
    if not lessons:
        return ""
    sections = [
        "# Relevant project lessons",
        "These are evidence-backed project observations, not measured improvements to "
        "harness reliability. Check applicability to the current task; current requirements "
        "take precedence. Do not generalize a snapshot into a long-lived cache.",
    ]
    for lesson in lessons:
        sections.append(f"## {lesson['id']} ({lesson['status']})")
        sections.extend(f"{key}: {lesson[key]}"
                        for key in ("cue", "lesson", "evidence", "confidence"))
    return "\n\n" + "\n\n".join(sections)
