"""Stable course/cloud grouping shared by tabs and export directories."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

from .resources import Resource


def resource_group(resource: Resource) -> tuple[str, str]:
    course_id = resource.metadata.get("courseId")
    name = str(resource.course or resource.metadata.get("courseName") or "").strip()
    if name or course_id not in (None, "", 0, "0"):
        if course_id not in (None, "", 0, "0"):
            school = resource.metadata.get("sid", resource.metadata.get("schoolUid", ""))
            key = "course:" + str(school) + ":" + str(course_id)
        else:
            key = "course-name:" + name
        return key, name or "未命名课程"
    if resource.source in ("cloud", "cloud_disk"):
        return "cloud", "个人云盘"
    if resource.source == "organization":
        return "organization", "机构云盘"
    return "unassigned", "未归属课程"


def _directory_name(name: str) -> str:
    cleaned = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).strip(" .") or "未归属课程"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", cleaned.split(".")[0]):
        cleaned = "_" + cleaned
    return cleaned[:72].rstrip(" .")


def group_labels(resources: list[Resource]) -> dict[str, str]:
    groups = dict(resource_group(resource) for resource in resources)
    counts: dict[str, int] = {}
    for label in groups.values():
        counts[label] = counts.get(label, 0) + 1
    return {key: label if counts[label] == 1 else label + " · " + hashlib.sha256(key.encode()).hexdigest()[:8]
            for key, label in groups.items()}


def resource_output_dir(output_root: str | Path, resource: Resource) -> Path:
    key, label = resource_group(resource)
    name = _directory_name(label)
    # Stable native identity distinguishes same-title courses even if each is
    # downloaded in a separate batch. Case/character/length normalization also
    # receives a stable suffix when it could collapse distinct display names.
    if key.startswith("course") or name != label:
        name += " · " + hashlib.sha256(key.encode()).hexdigest()[:8]
    return Path(output_root).expanduser().resolve() / name
