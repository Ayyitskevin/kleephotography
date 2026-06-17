"""Reference — Kevin's internal, for-his-eyes-only reference shelf.

NOT client-facing: no public route, no portal surface, no Send action. This is
where the rate card / pricing guides live (quoting-first business — every
[bracketed] figure is a rate still to set). The "Collections at a glance" table
reuses the same proposal presets the Templates gallery shows, so there is one
price story and no drift.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .. import security
from ..render import templates
from .doc_templates import _proposal_groups

log = logging.getLogger("mise.admin.reference")
router = APIRouter(prefix="/admin/reference",
                   dependencies=[Depends(security.require_admin)])


@router.get("", response_class=HTMLResponse)
async def reference(request: Request):
    return templates.TemplateResponse(
        request, "admin/reference.html",
        {"proposal_groups": _proposal_groups()})
