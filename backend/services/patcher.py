from __future__ import annotations

import re
from copy import deepcopy
from uuid import uuid4

from models import Resume, Section, Entry, Bullet
from services.llm import LLMClient


class EditError(RuntimeError):
    """An edit could not be completed reliably."""


class NoChangeError(EditError):
    """The requested edit returned the original resume unchanged."""


def uid() -> str:
    return uuid4().hex[:12]


def _find_section(resume: Resume, phrase: str) -> Section | None:
    phrase = phrase.strip().casefold()
    return next((s for s in resume.sections if phrase in s.name.casefold()), None)


def _without_ids(value):
    """Compare actual content, not generated IDs, to detect no-op edits."""
    if isinstance(value, dict):
        return {k: _without_ids(v) for k, v in value.items() if k != "id"}
    if isinstance(value, list):
        return [_without_ids(item) for item in value]
    return value


def _has_content_change(before: Resume, after: Resume) -> bool:
    return _without_ids(before.model_dump()) != _without_ids(after.model_dump())


def local_patch(resume: Resume, instruction: str) -> Resume:
    """Conservative offline fallback: only explicit add-bullet/add-section commands."""
    result = deepcopy(resume)
    text = instruction.strip()

    bullet = re.search(
        r"""add\s+(?:a\s+)?bullet\s+under\s+(.+?)\s+(?:about|saying|that says)\s+['"]?(.+?)['"]?\s*$""",
        text, re.I,
    )
    if bullet:
        target, value = bullet.group(1).strip(), bullet.group(2).strip().strip(" .'\"")
        section_name, separator, entry_name = target.partition(">")
        section = _find_section(result, section_name)
        if not section or not value:
            return result
        new_bullet = Bullet(id=uid(), text=value)
        if section.kind == "entries" and section.entries:
            needle = entry_name.strip().casefold()
            entry = next(
                (e for e in section.entries if needle and (
                    needle in e.title.casefold() or (e.org and needle in e.org.casefold())
                )),
                section.entries[-1] if not separator else None,
            )
            if entry is None:
                return result
            entry.bullets.append(new_bullet)
        elif section.kind == "bullets" and section.bullets is not None:
            section.bullets.append(new_bullet)
        else:
            return result
        return result

    add_section = re.search(r"add\s+(?:a\s+)?(.+?)\s+section(?:\s+after\s+(.+))?$", text, re.I)
    if add_section:
        name, after = add_section.group(1).strip().title(), add_section.group(2)
        if not _find_section(result, name):
            section = Section(id=uid(), name=name, kind="entries", entries=[])
            idx = next(
                (i + 1 for i, existing in enumerate(result.sections)
                 if after and after.casefold() in existing.name.casefold()),
                len(result.sections),
            )
            result.sections.insert(idx, section)
        return result

    return result


def apply_instruction(resume: Resume, instruction: str, llm: LLMClient | None) -> Resume:
    if not instruction.strip():
        raise EditError("Please provide an editing instruction.")

    if llm is not None:
        try:
            updated = llm.edit(resume, instruction)
        except Exception as exc:
            # Don't silently pretend a failed AI call was a successful local edit.
            raise EditError(f"AI editing failed: {type(exc).__name__}: {exc}") from exc

        if updated.id != resume.id:
            raise EditError("The AI response changed the resume ID; the edit was rejected.")
        if not _has_content_change(resume, updated):
            raise NoChangeError(
                "No changes were made. Try a more specific instruction, or check the AI configuration."
            )
        return updated

    updated = local_patch(resume, instruction)
    if not _has_content_change(resume, updated):
        raise NoChangeError(
            "No changes were made. Without an AI API key, only explicit add-bullet/add-section commands are supported."
        )
    return updated
