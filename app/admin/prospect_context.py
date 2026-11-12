"""Validated review navigation. Origin selects a return view, never a write target."""

import re
from urllib.parse import urlencode

from fastapi import HTTPException

from ..redirects import redirect_local


def review_context(request):
    review = request.query_params.get("review", "")
    if review not in {"due", "upcoming", "unplanned"}:
        return {}
    q = request.query_params.get("q", "").strip()[:200]
    params = {"review": review, "q": q}
    context = {}
    # Navigation only: never accept an arbitrary return URL or use this id for writes.
    if "review_origin" in request.query_params:
        origin = request.query_params["review_origin"]
        if not re.fullmatch(r"[0-9]{1,18}", origin) or int(origin) < 1:
            return {}
        context["review_origin"] = int(origin)
        params["review_origin"] = str(int(origin))
    return {**context, "review_query": "?" + urlencode(params)}


def redirect_to_prospect(prospect_id, request=None):
    """Back to this prospect. Only a numeric id may be part of the path.

    Anything else, including a scheme or a backslash host, stays on the list.
    ``redirect_local`` still refuses a target that is not a same-site path.
    """
    default = "/admin/studio/prospects"
    if isinstance(prospect_id, bool) or not isinstance(prospect_id, int):
        return redirect_local(default)
    if request is not None and (context := review_context(request)):
        return redirect_local(
            default
            + "/review-work"
            + context["review_query"]
            + f"&prospect_id={context.get('review_origin', prospect_id)}"
        )
    try:
        return redirect_local(f"{default}/{prospect_id}")
    except HTTPException:
        return redirect_local(default)
