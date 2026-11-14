"""Original LMS material attachments, derived from the shipped public frontend.

The legacy LMS endpoints require SID and login_uid in addition to the current
session signature. Attachment fileSize is in KiB; getFilesInfo.file_size is
rounded KiB and must never become a strict expected byte count.
"""

from dataclasses import replace
from decimal import Decimal, InvalidOperation
import json
from pathlib import PurePosixPath
from typing import Any

from .resources import Resource
from .session import SessionError


MATERIAL_DETAIL = "/lms/app/activity/learningMaterials/get"
FILE_INFO = "/lms/app/file/getFilesInfo"
MAX_ATTACHMENTS = 1000
FILE_BATCH_SIZE = 100
MAX_ATTACHMENT_JSON = 2 * 1024 * 1024
VIDEO_EXTENSIONS = {"mp4", "3gp", "mpg", "mpeg", "3g2", "avi", "flv", "wmv", "h264", "m4v",
                    "mj2", "mov", "ogg", "ogv", "rm", "qt", "vob", "webm", "mkv", "m3u8", "pm3u8"}


class LmsMaterialError(RuntimeError):
    """A safe user-facing material failure without server text or private URLs."""


def _identifier(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    text = str(value)
    return text if text.isascii() and text.isdigit() and 0 < int(text) < 2 ** 64 else ""


def _data(session, path: str, fields: dict) -> Any:
    try:
        response = session.post_form(path, fields)
    except SessionError:
        raise
    except Exception:
        raise LmsMaterialError("课堂资料请求失败，请重新扫描后重试。") from None
    if not isinstance(response, dict):
        raise LmsMaterialError("课堂资料接口返回了无法识别的数据。")
    error = response.get("error_info")
    if not isinstance(error, dict) or str(error.get("errno")) != "1":
        raise LmsMaterialError("课堂资料已不可访问，或当前账号无权读取；请重新扫描。")
    return response.get("data")


def _context_fields(session, context: dict) -> dict:
    course_id = _identifier(context.get("courseId"))
    sid = _identifier(context.get("sid", context.get("schoolUid", context.get("SID"))))
    uid = _identifier(session.uid)
    if not course_id or not sid or not uid:
        raise LmsMaterialError("课堂资料缺少当前账号、课程或学校标识，请重新扫描。")
    fields = {"courseId": course_id, "SID": sid, "login_uid": uid}
    category_id = _identifier(context.get("categoryId"))
    if category_id:
        fields["categoryId"] = category_id
    return fields


def _role(context: dict) -> tuple[str, int]:
    role = context.get("role")
    if role is None:
        # The course-list membership enum is distinct from detail iJ.
        identity = context.get("identity")
        role = ({1: "student", 2: "student", 3: "teacher", 4: "teacherAssistant", 192: "manager"}.get(identity)
                if isinstance(identity, int) else None)
    if role in {"teacher", "teacherAssistant", "manager"}:
        return role, 1
    if role in {"student", "auditor", "publicStudent", "publicAuditor"}:
        return role, 2
    raise LmsMaterialError("课堂资料缺少当前角色标识，请重新扫描。")


def attachment_list(value: Any) -> list[dict]:
    """Decode only a bounded LMS media list; never recursively collect URLs."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        if len(value) > MAX_ATTACHMENT_JSON:
            raise LmsMaterialError("课堂资料附件列表过大，无法完整读取。")
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            raise LmsMaterialError("课堂资料附件列表无法解析。") from None
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS or any(not isinstance(item, dict) for item in value):
        raise LmsMaterialError("课堂资料附件列表格式无法识别。")
    return value


def _precise_attachment_size(value: Any) -> int:
    """Use the fractional upload size only when it yields an exact byte count.

    Whole KiB values may be legacy rounding. The downloader obtains the exact
    HTTP Content-Length when this returns zero, avoiding false completeness.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return 0
    try:
        kib = Decimal(str(value))
        byte_count = kib * 1024
        if (kib.is_finite() and kib > 0 and kib != kib.to_integral_value()
                and byte_count == byte_count.to_integral_value() and byte_count < 2 ** 63):
            return int(byte_count)
    except (InvalidOperation, ValueError, OverflowError):
        pass
    return 0


def _file_infos(session, file_ids: list[str], context: dict) -> dict[str, dict]:
    result = {}
    fields = _context_fields(session, context)
    for offset in range(0, len(file_ids), FILE_BATCH_SIZE):
        batch = file_ids[offset:offset + FILE_BATCH_SIZE]
        data = _data(session, FILE_INFO, {**fields, "appType": 1,
                                       "fileIds": json.dumps(batch, separators=(",", ":"))})
        if not isinstance(data, list) or len(data) > MAX_ATTACHMENTS:
            raise LmsMaterialError("课堂资料原文件列表格式无法识别。")
        allowed = set(batch)
        for row in data:
            if not isinstance(row, dict):
                raise LmsMaterialError("课堂资料原文件列表格式无法识别。")
            file_id = _identifier(row.get("file_id"))
            if file_id not in allowed or file_id in result:
                raise LmsMaterialError("课堂资料原文件标识与请求不一致，请重新扫描。")
            result[file_id] = row
    return result


def _original_url(session, info: dict) -> str:
    path = info.get("file_save_path")
    if not isinstance(path, str) or not path:
        return ""
    try:
        return session.format_static_url(path)
    except SessionError:
        raise LmsMaterialError("课堂资料原文件地址无效，请重新扫描。") from None


def _name_and_kind(attachment: dict, info: dict, file_id: str) -> tuple[str, str]:
    name = attachment.get("fileName") or info.get("file_save_name") or f"课堂资料-{file_id}"
    if not isinstance(name, str):
        name = f"课堂资料-{file_id}"
    extension = info.get("file_extension", "")
    extension = extension.lstrip(".").lower() if isinstance(extension, str) else ""
    if extension and extension.isascii() and extension.isalnum() and not PurePosixPath(name).suffix:
        name += "." + extension
    suffix = PurePosixPath(name).suffix.lstrip(".").lower()
    return name, "video" if extension in VIDEO_EXTENSIONS or suffix in VIDEO_EXTENSIONS else "file"


def fetch_attachment_resources(session, attachments: list[dict], context: dict, *,
                               adapter: str = "lms-material", source: str = "course") -> list[Resource]:
    """Resolve LMS fileId attachments; reusable by homework attachment adapters."""
    attachments = attachment_list(attachments)
    selected = {}
    for item in attachments:
        file_id = _identifier(item.get("fileId"))
        if file_id:
            selected.setdefault(file_id, item)
        elif not item.get("linkAddress"):
            raise LmsMaterialError("部分课堂资料缺少原文件标识，无法完整读取。")
    if not selected:
        return []
    fields = _context_fields(session, context)
    infos = _file_infos(session, list(selected), context)
    result = []
    for file_id, attachment in selected.items():
        info = infos.get(file_id, {})
        name, kind = _name_and_kind(attachment, info, file_id)
        url = _original_url(session, info)
        metadata = {key: context[key] for key in ("activityId", "categoryId", "unitId", "role", "identity", "bizId") if key in context}
        metadata.update({"adapter": adapter, "courseId": fields["courseId"], "sid": fields["SID"],
                         "fileId": file_id, "lmsFileId": _identifier(attachment.get("lmsFileId")),
                         "auth_uid": fields["login_uid"], "original_available": bool(url),
                         "cache_complete": False})
        if not url:
            metadata["resolve_status"] = "原文件地址暂不可用，可在下载前重新获取"
        size = _precise_attachment_size(attachment.get("fileSize"))
        metadata["exact_attachment_size"] = size
        activity_id = _identifier(context.get("activityId")) or "attachment"
        result.append(Resource(id=f"{adapter}:{fields['courseId']}:{activity_id}:{file_id}",
                               name=name, kind=kind, source=source, url=url, size=size,
                               course=str(context.get("course", "")), metadata=metadata))
    return result


def fetch_resources(session, activity: dict, context: dict) -> list[Resource]:
    """Read a type-5 learning-material activity and its exact original files."""
    if str(activity.get("type", activity.get("bizType"))) != "5":
        raise LmsMaterialError("所选活动不是课堂资料。")
    activity_id = _identifier(activity.get("activityId"))
    if not activity_id:
        raise LmsMaterialError("课堂资料缺少活动标识，请重新扫描。")
    combined = {**context, **{key: activity[key] for key in ("categoryId", "unitId", "role", "bizId") if key in activity},
                "activityId": activity_id}
    role, identity = _role(combined)
    combined["role"] = role
    fields = {**_context_fields(session, combined), "activityId": activity_id, "identity": identity}
    detail = _data(session, MATERIAL_DETAIL, fields)
    if not isinstance(detail, dict) or str(detail.get("bizType")) != "5":
        raise LmsMaterialError("课堂资料详情格式无法识别。")
    # The returned id is the activity id; bizId identifies its material record.
    if detail.get("id") is not None and _identifier(detail["id"]) != activity_id:
        raise LmsMaterialError("课堂资料活动标识已变化，请重新扫描。")
    attachments = []
    for key in ("image", "audio", "docs", "video"):
        attachments.extend(attachment_list(detail.get(key)))
    if len(attachments) > MAX_ATTACHMENTS:
        raise LmsMaterialError("课堂资料附件过多，无法完整读取。")
    return fetch_attachment_resources(session, attachments, combined)


def refresh_attachment(session, resource: Resource) -> Resource:
    """Refresh only the selected file's original URL before downloading it."""
    metadata = resource.metadata
    if _identifier(metadata.get("auth_uid")) != _identifier(session.uid):
        raise LmsMaterialError("ClassIn 当前账号已切换，请重新扫描后下载。")
    file_id = _identifier(metadata.get("fileId"))
    if not file_id:
        raise LmsMaterialError("课堂资料缺少原文件标识，请重新扫描。")
    info = _file_infos(session, [file_id], metadata).get(file_id, {})
    url = _original_url(session, info)
    if not url:
        raise LmsMaterialError("课堂资料原文件地址暂不可用，请稍后重新扫描。")
    _, kind = _name_and_kind({"fileName": resource.name}, info, file_id)
    return replace(resource, url=url, kind=kind,
                   metadata={**metadata, "original_available": True, "resolve_status": ""})


def resolve_resource(session, resource: Resource) -> Resource:
    if resource.metadata.get("adapter") != "lms-material":
        raise LmsMaterialError("所选资源不是课堂资料附件。")
    return refresh_attachment(session, resource)
