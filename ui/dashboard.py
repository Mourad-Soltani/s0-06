"""The dashboard's behaviour without any screen (S3-04): load the queue, keep a selection, show one draft, approve or reject.
Streamlit only draws what this holds. A rejected token raises LoggedOut so the screen can send the person back to sign in;
every other failure becomes a plain notice, and the queue is reloaded because the server's state may have changed."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ui.review_client import AuthError, ReviewApiError
from ui.view_model import REJECT_REASONS, DetailView, QueueRow, detail_view, friendly_error, queue_rows


class LoggedOut(Exception):
    pass


@dataclass(frozen=True)
class Notice:
    kind: str          # success | error | info
    text: str


class Dashboard:
    def __init__(self, client):
        self.client = client
        self.rows: List[QueueRow] = []
        self.selected: Optional[str] = None
        self.detail: Optional[DetailView] = None
        self._notice: Optional[Notice] = None

    def take_notice(self) -> Optional[Notice]:
        notice, self._notice = self._notice, None
        return notice

    def refresh(self) -> None:
        try:
            self.rows = queue_rows(self.client.list_actions())
        except AuthError:
            raise LoggedOut() from None
        except ReviewApiError as exc:
            self._notice = Notice("error", friendly_error(exc.code))
            return
        ids = [r.proposal_id for r in self.rows]
        if self.selected not in ids:
            self.selected = ids[0] if ids else None
        self._load_detail()

    def select(self, proposal_id: str) -> None:
        if proposal_id not in [r.proposal_id for r in self.rows]:
            self._notice = Notice("error", "That item is no longer in the queue.")
            return
        self.selected = proposal_id
        self._load_detail()

    def approve(self) -> None:
        if self.detail is None:
            self._notice = Notice("error", "Nothing is selected.")
            return
        if not self.detail.can_approve:                                       # the server would refuse as well
            self._notice = Notice("error", self.detail.approve_blocked_reason or friendly_error("NOT_APPROVABLE"))
            return
        note = "Approved. " + self.detail.tier_note
        self._act(lambda pid: self.client.approve(pid), note)

    def reject(self, reason_code: str) -> None:
        if reason_code not in dict(REJECT_REASONS):
            raise ValueError("unknown rejection reason")
        if self.detail is None:
            self._notice = Notice("error", "Nothing is selected.")
            return
        self._act(lambda pid: self.client.reject(pid, reason_code), "Rejected. Your reason has been recorded.")

    # ------------------------------------------------------------------ helpers
    def _act(self, call, success_text: str) -> None:
        pid = self.selected
        try:
            call(pid)
            notice = Notice("success", success_text)
        except AuthError:
            raise LoggedOut() from None
        except ReviewApiError as exc:
            notice = Notice("error", friendly_error(exc.code))
        self._notice = None
        self.refresh()                                                          # the server's state may have changed either way
        self._notice = notice                                                   # what happened to the action matters most

    def _load_detail(self) -> None:
        if self.selected is None:
            self.detail = None
            return
        try:
            self.detail = detail_view(self.client.get_action(self.selected))
        except AuthError:
            raise LoggedOut() from None
        except ReviewApiError as exc:
            self.detail = None
            self._notice = Notice("error", friendly_error(exc.code))
