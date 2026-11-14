"""Run current-login detection and print a credential-free diagnostic summary.

Usage: python -m classin_exporter.login_probe
The installed getter instructions are inspected on every run. This command
does not dump process memory, search for candidate tokens, or call an API.
"""
from __future__ import annotations

import json
import re

from .session import ClassInSession, SessionError, load_current_session


def summarize_login(session: ClassInSession) -> dict[str, object]:
    """Repeat the login proof and expose only availability, never field values."""
    session._check_login()
    result: dict[str, object] = session.diagnostics()
    result["current_login_verified"] = True
    result["services_use_https"] = (session.dynamic_host.startswith("https://")
                                    and session.static_host.startswith("https://"))
    version = session._client_version
    result["client_version"] = version if re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,4}", version) else "unknown"
    result["token_strategy"] = "native-or-exchange" if result["token_available"] else "exchange-on-request"
    return result


def main() -> int:
    session: ClassInSession | None = None
    result: dict[str, object]
    try:
        session = load_current_session()
        result = {"ok": True, **summarize_login(session)}
    except SessionError as error:
        result = {"ok": False, "message": str(error)}
    except Exception:
        result = {"ok": False, "message": "当前 ClassIn 登录检测未完成，请重新打开客户端后重试。"}
    finally:
        if session is not None:
            try:
                session.close()
            except SessionError:
                pass
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
