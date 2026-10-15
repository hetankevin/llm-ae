"""Bounded callers use an opener that never forwards credentials on redirects."""

from urllib.request import HTTPRedirectHandler, build_opener

from .config import AEError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AEError("API redirected the request; configure the service's final HTTPS endpoint")


def urlopen(request, timeout=30):
    return build_opener(NoRedirect()).open(request, timeout=timeout)
