"""Read-only homework attachments using the observed LMS homework detail API."""

from __future__ import annotations

import importlib
import json

from .resources import Resource


def _attachment_helper():
    return importlib.import_module(".lms_material", __package__)


def _attachment_rows(value):
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise RuntimeError("作业附件列表格式无效。") from None
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise RuntimeError("作业附件列表格式无效。")
    return value


def fetch_resources(session, activity: dict, context: dict) -> list[Resource]:
    activity_id = activity.get("activityId")
    sid = context.get("sid", context.get("schoolUid"))
    course_id = context.get("courseId")
    if any(value in (None, "", 0, "0") for value in (activity_id, sid, course_id)):
        raise RuntimeError("作业附件缺少学校、课程或活动标识。")
    fields = {"activityId": activity_id, "SID": sid, "login_uid": session.uid,
              "courseId": course_id, "identity": 2 if context.get("role") == "student" else 1}
    if context.get("categoryId") not in (None, "", 0, "0"):
        fields["categoryId"] = context["categoryId"]
    try:
        payload = session.post_form("/lms/app/activity/homework/get", fields)
        if not isinstance(payload, dict) or payload.get("error_info", {}).get("errno") not in (1, "1"):
            raise RuntimeError("Homework request failed")
        detail = payload.get("data")
        if not isinstance(detail, dict):
            raise RuntimeError("Missing homework detail")
    except Exception:
        raise RuntimeError("读取作业附件详情失败；未将失败当作空列表。") from None
    attachments = []
    for key in ("image", "audio", "video", "docs"):
        attachments.extend(_attachment_rows(detail.get(key)))
    return _attachment_helper().fetch_attachment_resources(session, attachments,
        {**context, "activityId": activity_id, "activityName": activity.get("name", "")},
        adapter="lms-homework", source="course-homework")


def resolve_resource(session, resource: Resource) -> Resource:
    return _attachment_helper().refresh_attachment(session, resource)
