"""Small shared records consumed by discovery, downloads and the desktop UI."""

from dataclasses import dataclass, field


@dataclass
class Resource:
    id: str
    name: str
    kind: str
    source: str
    url: str = ""
    size: int = 0
    course: str = ""
    local_path: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class ScanResult:
    resources: list[Resource]
    warnings: list[str]
    profile_path: str = ""
    session_status: str = ""


@dataclass
class DownloadResult:
    resource_id: str
    path: str
    source: str
    bytes_written: int
    sha256: str
