"""Course/cloud grouping and readable, collision-safe export directories."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from itertools import chain
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
    return cleaned[:72].rstrip(" .") or "未归属课程"


def _planned_group_names(resources: Iterable[Resource]) -> dict[str, tuple[str, str]]:
    groups = dict(resource_group(resource) for resource in resources)
    bases = {key: _directory_name(label) for key, label in groups.items()}
    # Reserve natural names before adding numbers, so a course already named
    # "数学 (2)" keeps its name when other courses are both named "数学".
    reserved = {name.casefold() for name in bases.values()}
    used: set[str] = set()
    names = {}
    for key in sorted(groups):
        base, label = bases[key], groups[key]
        directory, suffix = base, ""
        if directory.casefold() in used:
            number = 2
            while True:
                suffix = f" ({number})"
                directory = base[:72 - len(suffix)].rstrip(" .") + suffix
                if directory.casefold() not in used and directory.casefold() not in reserved:
                    break
                number += 1
        used.add(directory.casefold())
        names[key] = (label + suffix, directory)
    return names


def group_labels(resources: Iterable[Resource]) -> dict[str, str]:
    return {key: label for key, (label, _) in _planned_group_names(resources).items()}


def group_directory_names(resources: Iterable[Resource]) -> dict[str, str]:
    """Plan names once from the full scan, shared by every download batch."""
    return {key: directory for key, (_, directory) in _planned_group_names(resources).items()}


def resource_output_dir(output_root: str | Path, resource: Resource,
                        resources: Iterable[Resource] | None = None, *,
                        directory_names: Mapping[str, str] | None = None) -> Path:
    key, label = resource_group(resource)
    if directory_names is None:
        context = [resource] if resources is None else chain(resources, [resource])
        directory_names = group_directory_names(context)
    name = directory_names.get(key, _directory_name(label))
    return Path(output_root).expanduser().resolve() / name
