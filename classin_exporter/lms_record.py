"""LMS recording activities and their current original video resources.

Protocol evidence: analysis/lms_record/evidence.md. All activity identifiers and
resource URLs remain in the caller's current in-memory scan state.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .resources import Resource
from .session import SessionError


DETAIL_PATH = "/lms/app/activity/recordClass/get"


def _present(value: Any) -> bool:
    return value not in (None, "", 0, "0")


def _md5(value: Any) -> str:
    digest = str(value or "").strip().strip('"').lower()
    return digest if len(digest) == 32 and all(char in "0123456789abcdef" for char in digest) else ""


def _request(session: Any, activity: dict, context: dict) -> dict:
    activity_id = activity.get("activityId", context.get("activityId"))
    course_id = context.get("courseId", activity.get("courseId"))
    sid = context.get("sid", context.get("SID", context.get("schoolUid", activity.get("SID"))))
    if not all(_present(value) for value in (activity_id, course_id, sid)):
        raise SessionError("录播活动缺少课程或学校标识，请重新扫描。")
    # The observed middleware supplies SID and login_uid. Membership identity
    # and LMS clusterRole/classRole use different enums and are not conflated.
    return {"activityId": activity_id, "courseId": course_id, "SID": sid,
            "login_uid": session.uid, "getStuStatistic": 1}


def _videos(response: Any) -> list[dict]:
    error = response.get("error_info") if isinstance(response, dict) else None
    if not isinstance(error, dict) or error.get("errno") != 1:
        raise SessionError("ClassIn 录播活动详情不可用，请重新扫描。")
    data = response.get("data")
    if not isinstance(data, dict):
        raise SessionError("ClassIn 录播活动详情格式无法识别。")
    video = data.get("video", "[]")
    if isinstance(video, str):
        try:
            video = json.loads(video or "[]")
        except (ValueError, TypeError):
            raise SessionError("ClassIn 录播视频列表无法解析。") from None
    if not isinstance(video, list) or any(not isinstance(row, dict) for row in video):
        raise SessionError("ClassIn 录播视频列表格式无法识别。")
    return video


def _file_key(row: dict) -> str:
    for key in ("fileId", "file_id", "lmsFileId"):
        if _present(row.get(key)):
            return str(row[key])
    raise SessionError("ClassIn 录播视频缺少文件标识。")


def _playback_path(row: dict) -> tuple[str, str]:
    transition = row.get("transition_data")
    if isinstance(transition, str):
        try:
            transition = json.loads(transition or "{}")
        except ValueError:
            raise SessionError("ClassIn 录播播放信息无法解析。") from None
    if not isinstance(transition, dict):
        return "", ""
    output = transition.get("output")
    output = output.get("V360P") if isinstance(output, dict) else None
    base = row.get("transition_files")
    if not isinstance(output, dict) or not isinstance(base, str) or not base:
        return "", ""
    # V360P.path is the direct MP4 URL used by the observed legacy viewer.
    # m3u8 is the standard HLS variant exposed by the current viewer. The
    # proprietary pm3u8 variant is not substituted for a standard playlist.
    for field, stream_type in (("path", ""), ("m3u8", "hls")):
        suffix = output.get(field)
        if isinstance(suffix, str) and suffix:
            return base + suffix, stream_type
    return "", ""


def _resource(session: Any, row: dict, activity: dict, context: dict) -> Resource | None:
    if row.get("isDel") in (1, "1", True):
        return None
    file_key = _file_key(row)
    original = row.get("file_save_path")
    source_path = original if isinstance(original, str) and original else ""
    content_role, stream_type = "original", ""
    if not source_path:
        source_path, stream_type = _playback_path(row)
        content_role = "transcoded-playback"
    if not source_path:
        # An incomplete or proprietary-only transcode cannot be represented as
        # a downloadable original video.
        raise SessionError("ClassIn 录播视频未提供可下载原件或标准播放地址。")
    url = session.format_static_url(source_path)
    suffix = PurePosixPath(urlsplit(url).path).suffix
    name = str(row.get("fileName") or row.get("name") or row.get("file_name") or row.get("file_save_name") or "录播视频")
    extension = str(row.get("file_extension") or row.get("fileExt") or "").lstrip(".")
    if content_role == "original" and extension and not name.lower().endswith("." + extension.lower()):
        name += "." + extension
    elif content_role != "original" and suffix and not name.lower().endswith(suffix.lower()):
        name = PurePosixPath(name).stem + suffix
    request = _request(session, activity, context)
    metadata = {"adapter": "lms-record", "activityId": request["activityId"],
                "courseId": request["courseId"], "sid": request["SID"],
                "role": context.get("role", activity.get("role", "")),
                "record_file_key": file_key, "content_role": content_role,
                "original_available": content_role == "original"}
    for field in ("fileId", "file_id", "lmsFileId", "filePermission", "file_extension", "duration", "videoTime"):
        if field in row:
            metadata[field] = row[field]
    # A native original digest must never be applied to its playback transcode.
    digest = _md5(row.get("md5") or row.get("MD5")) if content_role == "original" else ""
    if digest:
        metadata["md5"] = digest
    if stream_type:
        metadata["stream_type"] = stream_type
    if row.get("file_size") is not None:
        # Native file_size is rounded KiB. Do not assert it is an exact original
        # byte count: the downloader verifies the actual HTTP Content-Length.
        metadata["approximate_size_kib"] = row["file_size"]
    identity = f"lms-record:{request['courseId']}:{request['activityId']}:{file_key}"
    return Resource(identity, name, "video", "course-record", url=url, size=0,
                    course=str(context.get("course", "")), metadata=metadata)


def fetch_resources(session: Any, activity: dict, context: dict) -> list[Resource]:
    """Read all video entries of one already-discovered recording activity."""
    if activity.get("type") not in (None, 4, "4"):
        return []
    response = session.post_json(DETAIL_PATH, _request(session, activity, context))
    result = []
    for row in _videos(response):
        resource = _resource(session, row, activity, context)
        if resource is not None:
            result.append(resource)
    return result


def _matching_cache(previous: Resource, current: Resource) -> dict:
    """Retain cache verification only while the refreshed body is identifiable."""
    old, fresh = previous.metadata, current.metadata
    if (not previous.local_path or previous.id != current.id
            or not old.get("content_role") or old.get("content_role") != fresh.get("content_role")):
        return {}
    # fileId identifies the activity entry; its backing native file identifiers
    # must agree as well before an old body can remain a download fallback.
    if any(str(old.get(key, "")) != str(fresh.get(key, ""))
           for key in ("record_file_key", "fileId", "file_id", "lmsFileId")):
        return {}
    digest = _md5(fresh.get("md5"))
    cache_digest = _md5(old.get("cache_md5"))
    previous_digest = _md5(old.get("md5"))
    if digest and any(value and value != digest for value in (cache_digest, previous_digest)):
        return {}
    # Keep query selectors in the URL identity. A matching current server MD5
    # can establish the same body across an otherwise changed download URL.
    same_url = bool(current.url and old.get("cache_original_url") == current.url)
    same_digest = bool(digest and digest == cache_digest)
    if not (same_url or same_digest):
        return {}
    cached = {key: value for key, value in old.items() if key.startswith("cache_")}
    cached["cache_same_original"] = True
    return cached


def resolve_resource(session: Any, resource: Resource) -> Resource:
    """Re-read current activity access and URL immediately before a download."""
    metadata = resource.metadata
    if metadata.get("adapter") != "lms-record":
        raise SessionError("资源不是 ClassIn 录播活动，请重新扫描。")
    activity = {"activityId": metadata.get("activityId"), "type": 4,
                "role": metadata.get("role", "")}
    context = {"courseId": metadata.get("courseId"), "sid": metadata.get("sid"),
               "role": metadata.get("role", ""), "course": resource.course}
    key = str(metadata.get("record_file_key", ""))
    for current in fetch_resources(session, activity, context):
        if current.metadata.get("record_file_key") == key:
            cache = _matching_cache(resource, current)
            return replace(current, id=resource.id, local_path=resource.local_path if cache else "",
                           metadata={**current.metadata, **cache})
    raise SessionError("该录播视频已移除或当前账号无权访问，请重新扫描。")
