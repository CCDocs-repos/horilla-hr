"""
Keeps the public late/out form anonymous: no sign-in header ever reaches the
Google-gate login on /attendance-notice/.

/attendance-notice/ is the one way into Horilla with no Google gate in front of
it, so nothing upstream overwrites a sign-in header the caller made up. The
CCDocs GsuiteGateAuthMiddleware logs in whoever X-Auth-Request-Email names (a
superuser included). This middleware runs first (horilla/horilla_apps.py puts
it at the top of MIDDLEWARE; apps.py checks that) and removes every
X-Auth-Request-* header, plus the header named by HORILLA_SSO_TRUST_HEADER, on
this path. Caddy's site-level `request_header -X-Auth-Request-*` lines do the
same job at the edge; this is the second lock, in case those lines are lost.
"""

import os
import posixpath
import re

PUBLIC_PREFIX = "/attendance-notice"
DOTTED_PATH = "horilla.ccdocs_attendance.middleware.PublicNoticeMiddleware"
GATE_NAME = "GsuiteGateAuthMiddleware"


def trust_header_key() -> str:
    """The request.META key the Google gate reads (same rule as the gate)."""
    name = os.getenv("HORILLA_SSO_TRUST_HEADER", "X-Auth-Request-Email")
    return "HTTP_" + name.upper().replace("-", "_")


def is_public_path(path: str) -> bool:
    """
    True for /attendance-notice and anything under it. Caddy matches paths
    without caring about case, so this does not either; it also checks the
    path with repeated slashes and dot segments cleaned up.
    """
    raw = (path or "").lower()
    cleaned = posixpath.normpath(re.sub(r"/+", "/", raw) or "/")
    return any(
        p == PUBLIC_PREFIX or p.startswith(PUBLIC_PREFIX + "/") for p in (raw, cleaned)
    )


class PublicNoticeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if is_public_path(request.path_info) or is_public_path(request.path):
            trust_key = trust_header_key()
            for key in [
                k
                for k in request.META
                if k.startswith("HTTP_X_AUTH_REQUEST_") or k == trust_key
            ]:
                del request.META[key]
        return self.get_response(request)
