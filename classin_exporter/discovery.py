"""Observed ClassIn resource protocols and read-only cache discovery.

API evidence is recorded in analysis/api_evidence.md. Signed resource URLs stay
in memory. A failed refresh never silently reuses an expired cached URL.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
from collections import deque
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .cache import discover_cache_resources, read_cache_payloads
from .resources import Resource, ScanResult


INITIAL_PAGE = {"SepIndex": 0, "DirIndex": 0, "FileIndex": 0, "Limit": 80,
                "End": False, "First": True}
MAX_PAGES = 1000
MAX_FOLDERS = 10000
VIDEO_EXTENSIONS = {"mp4", "webm", "mkv", "mov", "avi", "m3u8", "pm3u8"}
RESOURCE_FIELDS = {
    "uid", "UID", "userId", "userFileId", "originId", "fileId", "FileID", "FileId",
    "referenceType", "referenceId", "referenceID", "shareUid", "sid", "schoolUid",
    "organizationDiskId", "oid", "courseId", "lessonId", "classId", "fileSource",
    "resourceFileType", "fileType", "fileExt", "fileSizeNumber", "file_size_number", "fileSize",
    "size", "byteSize", "file_size", "md5", "MD5", "sha256", "allowOperate", "auth", "authID", "goodsId",
    "transitionState", "moderationState", "supportTransform", "alias", "fileName",
    "FileName", "path", "filePath", "folderId", "url", "URL", "Url", "Definition",
}


def load_current_session(install_dir=None, roaming_dir=None, local_dir=None):
    # Delay loading the native-session adapter so cache-only discovery works
    # even on a machine where the adapter or ClassIn is unavailable.
    from .session import load_current_session as load
    return load(install_dir=install_dir, roaming_dir=roaming_dir, local_dir=local_dir)


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _folder_failure_warning(path, source, course, error):
    """Keep safe directory status without echoing request URLs or identifiers."""
    from .downloader import DownloadCancelled
    if isinstance(error, (CancelledError, DownloadCancelled)):
        raise error
    if source == "course" and path == "/cloudspace/api/course/getSubFolderList":
        from .session import SessionError
        # The current transport exposes non-2xx status through this fixed,
        # credential-free message, not a response/status attribute. Match the
        # entire trusted message; never extract numbers from arbitrary errors.
        status = (re.fullmatch(r"ClassIn 服务返回 HTTP ([1-5][0-9]{2})。", str(error))
                  if isinstance(error, SessionError) else None)
        detail = f"（HTTP {status[1]}）" if status else ""
        return f"{course or '该课程'}：课程云盘补充目录未确认{detail}；保留已发现资料。"
    return f"{course or source}资源列表请求失败，保留已发现资源。"


def _http(value):
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in {"http", "https"} and parsed.hostname else ""
    except ValueError:
        return ""


def _url(node):
    if isinstance(node, str):
        return _http(node)
    if isinstance(node, dict):
        for key in ("filePath", "path", "url", "URL", "Url", "PlayUrl", "playUrl", "videoUrl"):
            found = _http(node.get(key))
            if found:
                return found
    return ""


def _success(payload):
    if not isinstance(payload, dict):
        return True
    error = payload.get("error_info")
    if isinstance(error, dict) and "errno" in error:
        return _int(error["errno"], -1) == 1
    if "code" in payload:
        return _int(payload["code"], -1) in {0, 1, 200}
    return True


def _playset(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [entry for entry in value if isinstance(entry, dict) and _url(entry)] if isinstance(value, list) else []


def resources_from_payload(payload, source="cloud", course="") -> list[Resource]:
    """Normalize manifests, native responses and frontend/lesson resource rows.

    Raw IDs remain separate: userFileId is the download identifier, originId is
    an original/reference identifier. Duplicate names are never deduplicated.
    """
    result: dict[str, Resource] = {}

    def visit(node, context):
        if isinstance(node, list):
            for child in node:
                visit(child, context)
            return
        if not isinstance(node, dict) or not _success(node):
            return
        ctx = dict(context)
        for key in ("courseId", "lessonId", "sid", "uid", "fileSource", "organizationDiskId"):
            if node.get(key) not in (None, ""):
                ctx[key] = node[key]
        ctx["course"] = node.get("courseName") or node.get("className") or ctx.get("course", "")
        props = node.get("props")
        if not isinstance(props, dict):
            props = {}
        fields = {**ctx, **{k: v for k, v in node.items() if k in RESOURCE_FIELDS},
                  **{k: v for k, v in props.items() if k in RESOURCE_FIELDS}}
        canonical = all(key in node for key in ("id", "name", "kind", "source"))
        plays = _playset(node.get("Playset", node.get("playset")))
        playback = max(plays, key=lambda item: _int(item.get("Definition", item.get("definition")))) if plays else None
        url = _url(playback) if playback else _url(node) or _url(props)
        name = node.get("name") or node.get("fileName") or node.get("FileName") or node.get("file_name") or props.get("fileName")
        is_folder = node.get("isFolder") is True or (not canonical and "folderName" in node and not name)
        file_marker = any(k in fields for k in ("userFileId", "fileId", "FileID", "fileExt")) or bool(url) or bool(plays)
        if canonical or ((name or plays) and file_marker and not is_folder):
            metadata = dict(node.get("metadata", {})) if isinstance(node.get("metadata"), dict) else {}
            metadata.update({k: v for k, v in fields.items() if k != "course"})
            if "referenceID" in metadata and "referenceId" not in metadata:
                metadata["referenceId"] = metadata["referenceID"]
            if "userFileId" not in metadata and not canonical:
                download_id = node.get("id") or props.get("id") or node.get("fileId")
                if download_id not in (None, ""):
                    metadata["userFileId"] = download_id
            ext = str(node.get("fileExt") or props.get("fileExt") or "").lstrip(".").lower()
            name = str(name or (ctx.get("course", "") + "课堂回放" if plays else "") or Path(urlsplit(url).path).name or "未命名资源")
            if ext and not name.lower().endswith("." + ext):
                name += "." + ext
            if not ext:
                ext = Path(name).suffix.lstrip(".").lower() or Path(urlsplit(url).path).suffix.lstrip(".").lower()
            replay = playback is not None or source == "replay" or node.get("kind") in {"replay", "recording"}
            kind = "replay" if replay else "video" if ext in VIDEO_EXTENSIONS else str(node.get("kind", "file"))
            row_source = str(node.get("source") or source)
            identity = metadata.get("userFileId") or metadata.get("FileId") or metadata.get("FileID") or metadata.get("fileId") or node.get("id")
            if canonical:
                resource_id = str(node["id"])
            else:
                owner = metadata.get("sid") or metadata.get("uid") or metadata.get("UID") or "unknown"
                if identity in (None, ""):
                    identity = hashlib.sha256((url + name).encode()).hexdigest()[:24]
                resource_id = f"{row_source}:{owner}:file:{identity}"
                if replay:
                    resource_id = f"replay:{metadata.get('lessonId', 'unknown')}:{identity}"
            if playback:
                metadata["Definition"] = playback.get("Definition", playback.get("definition", 0))
            # fileSizeNumber is a formatted KB value in current cloud payloads.
            # Keep the display hint but validate downloads against byte counts.
            size = next((_int(fields[k]) for k in ("size", "byteSize", "file_size") if k in fields),
                        _int(node.get("size", node.get("FileSize", 0))))
            result[resource_id] = Resource(resource_id, name, kind, row_source, url, max(0, size),
                str(node.get("course") or ctx.get("course", "")), str(node.get("local_path", "")), metadata)
            return
        for key, value in node.items():
            if key not in {"error_info", "metadata", "props", "Playset", "playset"}:
                visit(value, ctx)

    visit(payload, {"course": course})
    return list(result.values())


def _data(payload):
    if not _success(payload):
        raise RuntimeError("ClassIn 返回错误，资源列表或原文件地址不可用。")
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def _cursor(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return dict(value) if isinstance(value, dict) else None


def _folder_ids(data):
    if not isinstance(data, dict):
        return []
    return [row.get("folderId", row.get("id")) for row in data.get("folderList", [])
            if isinstance(row, dict) and row.get("folderId", row.get("id")) not in (None, "")]


def _original_key(resource):
    # A query can select the object itself, not merely authenticate a path.
    url = resource.url if _http(resource.url) else ""
    digest = str(resource.metadata.get("md5", resource.metadata.get("MD5", ""))).lower()
    return url, digest


def _merge_originals(resources):
    """Attach verified cache slices without collapsing course references."""
    resources = list(resources)
    result = [resource for resource in resources if resource.source != "cache-index"]
    unmatched = []

    def same_original(first, second):
        url, digest = _original_key(first)
        other_url, other_digest = _original_key(second)
        if digest and other_digest and digest != other_digest:
            return False
        return bool(url and url == other_url or digest and digest == other_digest)

    def cache_rank(resource):
        return (resource.metadata.get("cache_complete") is True,
                _int(resource.metadata.get("cache_length")))

    for cached in (resource for resource in resources if resource.source == "cache-index"):
        matches = [index for index, resource in enumerate(result) if same_original(cached, resource)]
        if not matches:
            duplicate = next((index for index, previous in enumerate(unmatched) if same_original(cached, previous)), None)
            if duplicate is None:
                unmatched.append(cached)
            elif cache_rank(cached) > cache_rank(unmatched[duplicate]):
                unmatched[duplicate] = cached
            continue
        for index in matches:
            resource = result[index]
            if resource.local_path and cache_rank(resource) >= cache_rank(cached):
                continue
            cache_metadata = {key: value for key, value in cached.metadata.items() if key.startswith("cache_")}
            cache_metadata["cache_same_original"] = True
            result[index] = replace(resource, local_path=cached.local_path or resource.local_path,
                                    metadata={**resource.metadata, **cache_metadata})
    return result + unmatched


class ClassInClient:
    def __init__(self, install_dir=None, roaming_dir=None, local_dir=None, *, session=None, max_workers=4):
        if type(max_workers) is not int or not 1 <= max_workers <= 16:
            raise ValueError("扫描线程数必须是 1 到 16 的整数。")
        self.install_dir = install_dir
        self.roaming_dir = roaming_dir
        self.local_dir = Path(local_dir or Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "ClassIn")
        self.auth_session = session
        self.max_workers = max_workers
        self._session_error = ""

    def _current_session(self):
        if self.auth_session is None:
            self.auth_session = load_current_session(self.install_dir, self.roaming_dir, self.local_dir)
        return self.auth_session

    @property
    def session(self):
        """The requests transport used by DownloadManager after resource refresh."""
        if self.auth_session is None:
            return None
        transport = getattr(self.auth_session, "session", None)
        if transport is not None:
            return transport
        return getattr(self.auth_session, "requests_session", self.auth_session)

    def _post(self, path, fields):
        session = self._current_session()
        return session.post_form(path, {"UID": session.uid, **fields})

    def _course_catalogue(self, warnings):
        """Current and archived account courses, using the observed home API."""
        session = self._current_session()
        result, complete = {}, True
        for states in ([0], [1, 2]):
            seen_pages, count = set(), 0
            for page in range(1, MAX_PAGES + 1):
                try:
                    data = _data(session.post_json("/course/app/member/course_list", {
                        "page": page, "pageSize": 40, "states": states,
                    }))
                    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                        raise ValueError("Invalid course list")
                    rows = [row for row in data["list"] if isinstance(row, dict)]
                    signature = tuple(str(row.get("courseId", "")) for row in rows)
                    total = _int(data.get("total"), -1)
                    if total < 0 or not rows and count < total:
                        raise ValueError("Incomplete course page")
                    if rows and signature in seen_pages:
                        warnings.append("课程分页重复，已停止循环；课程列表可能不完整。")
                        complete = False
                        break
                    seen_pages.add(signature)
                    for row in rows:
                        course_id = row.get("courseId")
                        if course_id not in (None, "", 0, "0"):
                            result[str(course_id)] = row
                    count += len(rows)
                    if count >= total:
                        break
                except Exception:
                    warnings.append("当前或归档课程列表请求失败；保留已发现课程，列表可能不完整。")
                    complete = False
                    break
            else:
                warnings.append("课程分页超过读取上限；列表可能不完整。")
                complete = False
        return list(result.values()), complete

    def _lesson_catalogue(self, course, warnings):
        """Read every category and continue each class-activity cursor."""
        session = self._current_session()
        sid = course.get("sid", course.get("schoolUid", course.get("schoolId")))
        course_id = course.get("courseId")
        if any(value in (None, "", 0, "0") for value in (sid, course_id)):
            warnings.append("部分课程缺少学校标识；课次列表可能不完整。")
            return []
        result = {}
        try:
            data = _data(session.post_json("/lms/app/category/list", {"SID": sid, "courseId": course_id}))
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise ValueError("Invalid category list")
            categories = data["list"]
        except Exception:
            warnings.append("部分课程的课次分类列表请求失败；课次列表可能不完整。")
            return []

        def retain(rows, category_id):
            for row in rows:
                lesson_id = row.get("bizId")
                if lesson_id not in (None, "", 0, "0"):
                    result[str(lesson_id)] = {**course, "sid": sid, "categoryId": category_id,
                                             "lessonId": lesson_id, "lessonName": row.get("name", ""),
                                             "activityId": row.get("activityId"),
                                             "recordState": row.get("recordState")}

        for category in categories:
            category_id = category.get("categoryId") if isinstance(category, dict) else None
            if category_id in (None, "", 0, "0"):
                warnings.append("部分课次分类缺少标识；课次列表可能不完整。")
                continue
            fields = {"SID": sid, "courseId": course_id, "categoryId": category_id}
            try:
                data = _data(session.post_json("/lms/app/course/classActivityList", fields))
                if not isinstance(data, dict):
                    raise ValueError("Invalid lesson list")
                for group, flag in (("notStarted", 0), ("ongoing", 1), ("end", 2)):
                    bucket = data.get(group)
                    if not isinstance(bucket, dict) or not isinstance(bucket.get("list"), list):
                        raise ValueError("Missing lesson group")
                    rows = [row for row in bucket["list"] if isinstance(row, dict)]
                    retain(rows, category_id)
                    count, total = len(rows), _int(bucket.get("total"), -1)
                    seen_cursors = set()
                    seen_activities = {str(row.get("activityId")) for row in rows}
                    for _page in range(MAX_PAGES):
                        if total < 0:
                            raise ValueError("Missing lesson total")
                        if count >= total:
                            break
                        cursor = rows[-1].get("activityId") if rows else None
                        if cursor in (None, "", 0, "0") or str(cursor) in seen_cursors:
                            raise ValueError("Invalid or repeated lesson cursor")
                        seen_cursors.add(str(cursor))
                        next_data = _data(session.post_json("/lms/app/course/classActivityList", {
                            **fields, "processFlag": flag, "limit": 100, "lastActivityId": cursor,
                        }))
                        next_bucket = next_data.get(group) if isinstance(next_data, dict) else None
                        if not isinstance(next_bucket, dict) or not isinstance(next_bucket.get("list"), list):
                            raise ValueError("Missing lesson page")
                        rows = [row for row in next_bucket["list"] if isinstance(row, dict)]
                        if not rows:
                            raise ValueError("Incomplete lesson page")
                        new_activities = {str(row.get("activityId")) for row in rows}
                        if not new_activities or new_activities & seen_activities:
                            raise ValueError("Repeated lesson page")
                        seen_activities.update(new_activities)
                        retain(rows, category_id)
                        count += len(rows)
                    else:
                        warnings.append("课次分页超过读取上限；回放列表可能不完整。")
            except Exception:
                warnings.append("部分课程的课次列表无法完整读取；保留已发现课次。")
        return list(result.values())

    def _activity_catalogue(self, course, warnings):
        """All LMS unit activities, including separately paged unit buckets."""
        session = self._current_session()
        sid = course.get("sid", course.get("schoolUid", course.get("schoolId")))
        course_id = course.get("courseId")
        role = {1: "student", 2: "student", 3: "teacher", 4: "teacherAssistant", 192: "manager"}.get(_int(course.get("identity")))
        if not role or any(value in (None, "", 0, "0") for value in (sid, course_id)):
            warnings.append("部分课程缺少学校或角色标识；课程活动资料列表可能不完整。")
            return []
        result = {}
        base = {"SID": sid, "courseId": course_id, "role": role, "sort": "asc", "isUpcoming": 0, "isSearch": 0}
        suffix = "student" if role == "student" else ""
        unit_path = "/lms/app/course/" + ("studentUnitList" if suffix else "unitList")
        activity_path = "/lms/app/course/" + ("studentUnitActivityList" if suffix else "unitActivityList")
        try:
            data = _data(session.post_json("/lms/app/category/list", {"SID": sid, "courseId": course_id}))
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise ValueError("Invalid categories")
        except Exception:
            warnings.append("部分课程的活动分类列表请求失败；资料列表可能不完整。")
            return []
        for category in data["list"]:
            category_id = category.get("categoryId") if isinstance(category, dict) else None
            if category_id in (None, "", 0, "0"):
                warnings.append("部分课程活动分类缺少标识；资料列表可能不完整。")
                continue
            fields = {**base, "categoryId": category_id}
            try:
                unit_data = _data(session.post_json(unit_path, fields))
                if not isinstance(unit_data, dict) or not isinstance(unit_data.get("list"), list):
                    raise ValueError("Invalid units")
                unit_ids = list(dict.fromkeys(row["unitId"] for row in unit_data["list"]
                    if isinstance(row, dict) and row.get("unitId") not in (None, "", 0, "0")))
                for start in range(0, len(unit_ids), 10):
                    batch = unit_ids[start:start + 10]
                    payload = _data(session.post_json(activity_path, {
                        **fields, "isSearch": 1, "unitIds": batch, "limit": 100, "offset": 0,
                    }))
                    if not isinstance(payload, dict) or not isinstance(payload.get("list"), list):
                        raise ValueError("Invalid activities")
                    buckets = [bucket for bucket in payload["list"] if isinstance(bucket, dict)
                               and str(bucket.get("unitId")) in {str(unit) for unit in batch}]
                    for initial in buckets:
                        unit_id = initial.get("unitId")
                        bucket, seen_activities = initial, set()
                        for page in range(1, MAX_PAGES + 1):
                            if not isinstance(bucket.get("activities"), list):
                                raise ValueError("Invalid activity bucket")
                            rows = [row for row in bucket["activities"] if isinstance(row, dict)]
                            page_ids = {str(row.get("activityId")) for row in rows}
                            if page > 1 and (not rows or page_ids & seen_activities):
                                raise ValueError("Repeated or incomplete activity page")
                            seen_activities.update(page_ids)
                            for row in rows:
                                activity_id = row.get("activityId")
                                if activity_id not in (None, "", 0, "0"):
                                    result[str(activity_id)] = {**course, "sid": sid, "schoolUid": sid,
                                        "role": role, "categoryId": category_id, "unitId": unit_id, **row}
                            page_total = _int(bucket.get("pageTotal"), -1)
                            if page_total < 0:
                                raise ValueError("Missing activity page total")
                            if page >= page_total:
                                break
                            next_data = _data(session.post_json(activity_path, {
                                **fields, "isSearch": 1, "unitIds": [unit_id], "limit": 100, "offset": page * 100,
                            }))
                            next_buckets = next_data.get("list", []) if isinstance(next_data, dict) else []
                            bucket = next((item for item in next_buckets if isinstance(item, dict)
                                and str(item.get("unitId")) == str(unit_id)), {})
                        else:
                            warnings.append("课程活动分页超过读取上限；资料列表可能不完整。")
            except Exception:
                warnings.append("部分课程活动列表无法完整读取；保留已发现的资料与录播活动。")
        return list(result.values())

    def _fetch_activity_resources(self, activity):
        module = {2: "lms_homework", 4: "lms_record", 5: "lms_material"}.get(_int(activity.get("type")))
        if module is None:
            return []
        context = {key: activity[key] for key in ("courseId", "sid", "schoolUid", "course", "identity",
                   "role", "categoryId", "unitId") if key in activity}
        adapter = importlib.import_module("." + module, __package__)
        return adapter.fetch_resources(self._current_session(), activity, context)

    def _walk(self, path, fields, source, course, warnings, resources, *, paginated=True):
        queue = deque([fields.get("folderId", 0)])
        visited = set()
        for _ in range(MAX_FOLDERS):
            if not queue:
                return
            folder = queue.popleft()
            if str(folder) in visited:
                continue
            visited.add(str(folder))
            cursor = dict(INITIAL_PAGE) if paginated else None
            seen_cursors = set()
            for _page in range(MAX_PAGES):
                request = {**fields, "folderId": folder}
                if cursor is not None:
                    request["pageInfo"] = json.dumps(cursor, separators=(",", ":"))
                    signature = json.dumps(cursor, sort_keys=True, separators=(",", ":"))
                    if signature in seen_cursors:
                        warnings.append("资源分页游标重复，已停止循环；列表可能不完整。")
                        break
                    seen_cursors.add(signature)
                try:
                    payload = self._post(path, request)
                    data = _data(payload)
                except Exception as error:
                    warnings.append(_folder_failure_warning(path, source, course, error))
                    break
                context = {**request, "uid": self._current_session().uid}
                if source == "organization":
                    context["fileSource"] = 8
                normalized = {"data": {**context, **data}} if isinstance(data, dict) else {"data": data, **context}
                for resource in resources_from_payload(normalized, source, course):
                    resources[resource.id] = resource
                queue.extend(child for child in _folder_ids(data) if str(child) not in visited)
                if not paginated:
                    break
                next_cursor = _cursor(data.get("pageInfo")) if isinstance(data, dict) else None
                if next_cursor is None:
                    # Older deployments do not paginate the personal folder API.
                    break
                if next_cursor.get("End") is True or str(next_cursor.get("End")).lower() in {"true", "1"}:
                    break
                cursor = next_cursor
            else:
                warnings.append("资源分页超过读取上限；列表可能不完整。")
        if queue:
            warnings.append("资源文件夹超过读取上限；列表可能不完整。")

    def _scan_folder(self, path, fields, source, course, *, paginated=True, context=None):
        warnings, resources = [], {}
        try:
            self._walk(path, fields, source, course, warnings, resources, paginated=paginated)
        except Exception as error:
            warnings.append(_folder_failure_warning(path, source, course, error))
        rows = list(resources.values())
        if context:
            rows = [replace(row, metadata={**context, **row.metadata}) for row in rows]
        return rows, warnings

    def _scan_organization(self, sid):
        warnings, resources = [], {}
        try:
            data = _data(self._post("/cloudspace/api/organization/disk/list", {"sid": sid}))
            disks = data.get("diskList", []) if isinstance(data, dict) else []
            for disk in disks:
                disk_id = disk.get("organizationDiskId") if isinstance(disk, dict) else None
                if disk_id in (None, ""):
                    continue
                self._walk("/cloudspace/api/organization/folder/list", {"folderId": disk_id,
                    "sid": sid, "organizationDiskId": disk_id, "cate": 2, "order": 0,
                    "resourceFileType": 3, "appType": 1}, "organization", "", warnings, resources)
        except Exception:
            warnings.append("组织云盘列表请求失败，保留已发现资源。")
        return list(resources.values()), warnings

    def _scan_course(self, evidence):
        warnings, lessons, activities = [], [], []
        try:
            lessons = self._lesson_catalogue(evidence, warnings)
        except Exception:
            warnings.append("部分课程的课次列表无法完整读取；保留已发现课次。")
        try:
            activities = self._activity_catalogue(evidence, warnings)
        except Exception:
            warnings.append("部分课程活动列表无法完整读取；保留已发现的资料与录播活动。")
        return lessons, activities, warnings

    def _scan_activity(self, activity):
        warnings, resources = [], []
        try:
            for resource in self._fetch_activity_resources(activity):
                resources.append(resource)
        except Exception:
            warnings.append("部分课程资料或录播详情无法读取；保留已发现资源。")
        return resources, warnings

    def _scan_replay(self, lesson):
        warnings, resources = [], []
        try:
            payload = self._replay_payload(lesson)
            if isinstance(payload, dict) and _int(payload.get("error_info", {}).get("errno")) == 809:
                candidate_id = f"replay-candidate:{lesson.get('sid', '')}:{lesson.get('courseId', '')}:{lesson['lessonId']}"
                resources.append(Resource(candidate_id,
                    lesson.get("lessonName") or (lesson.get("course", "") + "课堂回放入口"), "replay", "replay",
                    course=lesson.get("course", ""), metadata={**lesson, "candidate_only": True,
                    "availability": "denied", "record_exists": None, "last_errno": 809,
                    "status": "无回放读取权限（809）；尚未确认存在可下载录像。"}))
                warnings.append("部分课堂回放入口没有读取权限（809）；仅显示候选入口，未确认录像存在。")
            else:
                context = {"data": {**lesson, **_data(payload)}}
                resources.extend(resources_from_payload(context, "replay", lesson.get("course", "")))
        except Exception:
            warnings.append("部分课堂回放列表无法读取；未将失败当作空列表。")
        return resources, warnings

    def scan(self) -> ScanResult:
        warnings, resources = [], {}
        session = None
        try:
            session = self._current_session()
        except Exception:
            warnings.append("无法读取当前 ClassIn 登录状态，仅显示本地历史缓存。")
        cache_root = self.local_dir / "cache"
        if session is not None:
            active_cache = getattr(session, "cache_directory", None)
            profiles = [Path(active_cache)] if active_cache else []
            profile_path = str(profiles[0]) if profiles else ""
            if not profiles:
                warnings.append("无法确认当前 ClassIn 缓存目录。")
            status = "已读取当前 ClassIn 登录状态"
        else:
            profiles = sorted(path for path in cache_root.glob("*") if path.is_dir() and path.name.isdigit())
            profile_path = str(profiles[0]) if len(profiles) == 1 else ""
            status = "未确认当前登录状态（历史缓存）"
        courses, schools, lessons = {}, set(), {}

        def index(node, context=None):
            context = context or {}
            if isinstance(node, list):
                for child in node:
                    index(child, context)
                return
            if not isinstance(node, dict) or not _success(node):
                return
            ctx = {**context}
            cid = node.get("courseId", node.get("CourseID", ctx.get("courseId")))
            cname = node.get("courseName", node.get("className", ctx.get("course", "")))
            if cid not in (None, "", 0, "0"):
                ctx.update(courseId=cid, course=cname)
            if "identity" in node:
                ctx["identity"] = node["identity"]
            sid = node.get("sid", node.get("schoolUid", node.get("schoolId", ctx.get("sid"))))
            if sid not in (None, "", 0, "0"):
                schools.add(sid)
                ctx["sid"] = sid
            if cid not in (None, "", 0, "0"):
                courses[str(cid)] = {**courses.get(str(cid), {}), **ctx,
                                     "identity": ctx.get("identity")}
            lesson = node.get("lessonId", node.get("LessonID"))
            if lesson not in (None, "", 0, "0"):
                lessons[str(lesson)] = {**ctx, "lessonId": lesson}
            for value in node.values():
                if isinstance(value, (list, dict)):
                    index(value, ctx)

        for profile in profiles:
            try:
                for resource in discover_cache_resources(profile):
                    resources[resource.id] = resource
                for url, payload in read_cache_payloads(profile):
                    index(payload)
                    query = parse_qs(urlsplit(url).query)
                    for field in ("courseId", "lessonId"):
                        value = query.get(field, [None])[0]
                        if value:
                            index({field: value})
                    path = urlsplit(url).path
                    cached_source = "replay" if "getLessonRecordInfo" in url else "course" if "/course/" in path and "Folder" in path else "organization" if "/organization/" in path else "cloud" if "/cloudspace/api/user/" in path else "cached-api"
                    context = {"uid": session.uid} if session is not None else {}
                    if isinstance(payload, dict):
                        payload = {**context, **payload}
                    for resource in resources_from_payload(payload, cached_source):
                        resources[resource.id] = resource
            except Exception:
                warnings.append("部分本地缓存无法读取，已保留可读取的资源。")
        if session is not None:
            def read_courses():
                local_warnings = []
                try:
                    rows, complete = self._course_catalogue(local_warnings)
                except Exception:
                    rows, complete = [], False
                    local_warnings.append("当前或归档课程列表请求失败；保留已发现课程，列表可能不完整。")
                return rows, complete, local_warnings

            def collect(future):
                rows, local_warnings = future.result()
                warnings.extend(local_warnings)
                for resource in rows:
                    resources[resource.id] = resource

            def run_task(task, args, kwargs):
                result = None
                try:
                    result = task(*args, **kwargs)
                    return result
                finally:
                    release = getattr(session, "release_thread_transport", None)
                    if callable(release):
                        try:
                            release()
                        except Exception:
                            # All task result tuples end with task-local
                            # warnings; only the coordinator merges this list.
                            if isinstance(result, tuple) and isinstance(result[-1], list):
                                result[-1].append("部分网络连接清理失败，请重新扫描。")

            # One pool bounds all request stages. Only the calling coordinator
            # merges scan state; each task owns its warnings and result buffers.
            with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="classin-scan") as executor:
                def submit(task, *args, **kwargs):
                    return executor.submit(run_task, task, args, kwargs)

                catalogue_future = submit(read_courses)
                cloud_future = submit(self._scan_folder, "/cloudspace/api/user/getFolderList",
                    {"folderId": 0, "cate": 2, "order": 0, "resourceFileType": 3,
                     "language": "cn", "appType": 1}, "cloud", "")
                current_courses, catalogue_complete, catalogue_warnings = catalogue_future.result()
                warnings.extend(catalogue_warnings)
                for current_course in current_courses:
                    index(current_course)
                collect(cloud_future)

                organization_futures = [submit(self._scan_organization, sid)
                                        for sid in sorted(schools, key=str)]
                course_futures = [(evidence, submit(self._scan_course, evidence))
                                  for evidence in courses.values()]
                for future in organization_futures:
                    collect(future)
                course_batches = []
                for evidence, future in course_futures:
                    course_lessons, activities, course_warnings = future.result()
                    for lesson in course_lessons:
                        lessons[str(lesson["lessonId"])] = lesson
                    course_batches.append((evidence, activities, course_warnings))

                pending_courses = []
                for evidence, activities, course_warnings in course_batches:
                    detail_futures = [submit(self._scan_activity, activity) for activity in activities]
                    context = {key: evidence[key] for key in ("courseId", "sid") if key in evidence}
                    folder_future = submit(self._scan_folder, "/cloudspace/api/course/getSubFolderList",
                        {"folderId": 0, "courseId": evidence["courseId"]}, "course", evidence["course"],
                        paginated=False, context=context)
                    pending_courses.append((detail_futures, folder_future, course_warnings))
                replay_futures = [submit(self._scan_replay, lesson) for lesson in lessons.values()]
                # Consume in discovery order, not completion order, so output
                # and warning ordering remain stable under different timings.
                for detail_futures, folder_future, course_warnings in pending_courses:
                    warnings.extend(course_warnings)
                    for future in detail_futures:
                        collect(future)
                    collect(folder_future)
                if not courses and not catalogue_complete:
                    warnings.append("未发现可确认的课程索引；课程资料与课堂回放列表可能不完整。")
                for future in replay_futures:
                    collect(future)
        return ScanResult(_merge_originals(resources.values()), list(dict.fromkeys(warnings)), profile_path, status)

    def _replay_payload(self, metadata):
        session = self._current_session()
        sid = metadata.get("sid", metadata.get("schoolUid", metadata.get("schoolId")))
        course_id, lesson_id = metadata.get("courseId"), metadata.get("lessonId")
        if any(value in (None, "", 0, "0") for value in (sid, course_id, lesson_id)):
            raise RuntimeError("课堂回放缺少可确认的学校、课程或课次标识。")
        return session.post_form("/api/classin.api.php?action=getLessonRecordInfo", {
            "SID": sid, "clientCourseId": course_id, "clientClassId": lesson_id,
            "memberUid": session.uid, "login_uid": session.uid, "flag": 1,
        })

    def resolve(self, resource: Resource) -> Resource:
        session = self._current_session()
        meta = resource.metadata
        if meta.get("candidate_only"):
            raise RuntimeError("该课堂回放入口没有当前可读取的录像地址，不能下载。")
        adapter_name = meta.get("adapter")
        adapter = ({"lms-record": "lms_record", "lms-material": "lms_material",
                    "lms-homework": "lms_homework"}.get(adapter_name)
                   if isinstance(adapter_name, str) else None)
        if adapter:
            module = importlib.import_module("." + adapter, __package__)
            return module.resolve_resource(session, resource)
        file_id = meta.get("userFileId")
        source = _int(meta.get("fileSource"))
        reference = _int(meta.get("referenceType"))
        if resource.kind == "replay":
            try:
                payload = self._replay_payload(meta)
                data = _data(payload)
                rows = resources_from_payload({"data": {**meta, **data}}, "replay", resource.course)
                record_id = meta.get("FileId", meta.get("FileID", meta.get("fileId")))
                if record_id is not None:
                    rows = [row for row in rows if str(row.metadata.get("FileId", row.metadata.get("FileID", row.metadata.get("fileId")))) == str(record_id)]
                if len(rows) != 1 or not rows[0].url:
                    raise RuntimeError("ambiguous replay")
                return replace(resource, url=rows[0].url, metadata={**meta, **rows[0].metadata, "remote_refreshed": True})
            except Exception:
                raise RuntimeError("刷新课堂回放地址失败；未使用旧的缓存地址。") from None
        if source == 8 or source == 6 and reference == 8 or resource.source == "organization":
            if file_id in (None, "") or meta.get("sid") in (None, ""):
                raise RuntimeError("组织资源缺少可确认的文件或机构标识。")
            path, fields = "/cloudspace/api/organization/download", {"fileIds": file_id, "sid": meta["sid"]}
        elif source == 6 and reference == 6:
            if meta.get("referenceId") in (None, "") or meta.get("shareUid") in (None, ""):
                raise RuntimeError("共享资源缺少可确认的共享标识。")
            path, fields = "/clouddisk/api/downloadSharedFile", {"shareId": meta["referenceId"], "shareUid": meta["shareUid"]}
        elif resource.source == "course" or meta.get("courseId") not in (None, "", 0, "0"):
            if file_id in (None, "") or meta.get("courseId") in (None, ""):
                raise RuntimeError("课程资源缺少可确认的文件或课程标识。")
            path, fields = "/clouddisk/api/course/getSourceFilePath", {"courseFileId": file_id, "courseId": meta["courseId"]}
            telephone = getattr(session, "telephone", "")
            if telephone:
                fields["telephone"] = telephone
            if meta.get("lessonId") not in (None, "", 0, "0"):
                fields["clientClassId"] = meta["lessonId"]
            if meta.get("goodsId") not in (None, "", 0, "0"):
                fields["goodsId"] = meta["goodsId"]
        else:
            if file_id in (None, ""):
                raise RuntimeError("资源仅有缓存地址，缺少刷新原文件所需的文件标识。")
            path, fields = "/clouddisk/api/downloadSourceFile", {"fileId": file_id, "isNew": "1"}
        try:
            data = _data(self._post(path, fields))
            candidates = data if isinstance(data, list) else [data]
            fresh_url = next((_url(row) for row in candidates if _url(row)), "")
            if not fresh_url:
                formatter = getattr(session, "format_static_url", None)
                for row in candidates:
                    relative = row if isinstance(row, str) else row.get("filePath", row.get("path", "")) if isinstance(row, dict) else ""
                    if not isinstance(relative, str) or not relative or not callable(formatter):
                        continue
                    parsed = urlsplit(relative)
                    if parsed.scheme or parsed.netloc or relative.startswith("\\"):
                        continue
                    fresh_url = _http(formatter(relative))
                    if fresh_url:
                        break
            if not fresh_url:
                raise RuntimeError("no original URL")
        except Exception:
            raise RuntimeError("刷新原文件地址失败；未使用旧的缓存地址。") from None
        cache_url = meta.get("cache_original_url", "")
        same_cache = bool(_http(cache_url) and _original_key(Resource("", "", "", "", cache_url))[0]
                          == _original_key(Resource("", "", "", "", fresh_url))[0])
        return replace(resource, url=fresh_url, metadata={**meta, "remote_refreshed": True,
                       "cache_same_original": same_cache})
