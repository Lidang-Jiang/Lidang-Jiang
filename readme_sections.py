"""Safely replace generated sections in a profile README."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path


def replace_section(readme: str, section: str, content: str) -> str:
    """Replace content between START/END markers for a given section."""
    start_marker = f"<!-- START_SECTION:{section} -->"
    end_marker = f"<!-- END_SECTION:{section} -->"
    if readme.count(start_marker) != 1 or readme.count(end_marker) != 1:
        raise RuntimeError(f"Missing or duplicate generated section: {section}")
    pattern = rf"({re.escape(start_marker)})\n.*?({re.escape(end_marker)})"

    def replacement(match: re.Match[str]) -> str:
        return f"{match.group(1)}\n{content}\n{match.group(2)}"

    updated, replacement_count = re.subn(
        pattern,
        replacement,
        readme,
        flags=re.DOTALL,
    )
    if replacement_count != 1:
        raise RuntimeError(f"Missing or duplicate generated section: {section}")
    return updated


def upsert_section_after(
    readme: str,
    section: str,
    content: str,
    after_section: str,
) -> str:
    """Replace a generated section or insert it after another generated section."""
    start_marker = f"<!-- START_SECTION:{section} -->"
    end_marker = f"<!-- END_SECTION:{after_section} -->"
    block = f"{start_marker}\n{content}\n<!-- END_SECTION:{section} -->"

    if start_marker in readme:
        return replace_section(readme, section, content)
    if readme.count(end_marker) != 1:
        raise RuntimeError(f"Missing marker or duplicate marker: {end_marker}")
    return readme.replace(end_marker, f"{end_marker}\n\n{block}", 1)


def write_readme_sections(
    path: Path,
    summary: str,
    table: str,
    commit_details: str,
    pr_details: str,
) -> None:
    """Write every generated README section atomically from complete content."""
    readme = path.read_text(encoding="utf-8")
    readme = replace_section(readme, "summary", summary)
    readme = replace_section(readme, "contributions", table)
    readme = upsert_section_after(
        readme,
        "commit_details",
        commit_details,
        "contributions",
    )
    readme = upsert_section_after(
        readme,
        "pr_details",
        pr_details,
        "commit_details",
    )
    content = readme.rstrip() + "\n"
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.chmod(stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
