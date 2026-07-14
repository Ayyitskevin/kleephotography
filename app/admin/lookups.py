"""Neutral Studio record lookups shared by admin routers."""

from .. import db

PROJECT_STATUSES = [
    "inquiry_received",
    "consultation_call",
    "proposal_sent",
    "contract_signed",
    "retainer_paid",
    "session_planning",
    "project_closed",
    "archived",
]


def get_client(client_id: int) -> "db.sqlite3.Row":
    return db.get_or_404("SELECT * FROM clients WHERE id=?", (client_id,))


def get_project(project_id: int) -> "db.sqlite3.Row":
    return db.get_or_404(
        """SELECT p.*, c.name AS client_name, c.company, c.email AS client_email
           FROM projects p JOIN clients c ON c.id=p.client_id WHERE p.id=?""",
        (project_id,),
    )
