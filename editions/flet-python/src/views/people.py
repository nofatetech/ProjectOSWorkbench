"""PeopleView — the 40_People/ vault-browser surface.

First view extracted from WorkbenchApp (refactor pilot, 2026-06-13). It owns its
own controls (list / preview / filter) and view-logic; shared browser machinery
(`_build_browser_view_body`, `_build_browser_sidebar_row`, `_refresh_browser_*`)
stays on the app and is called via `self.app`, since People and Resources share
it — generalizing those into a base lives in a later step, not this pilot.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from models import OpenTab, VaultEntry

if TYPE_CHECKING:
    from main import WorkbenchApp


class PeopleView:
    def __init__(self, app: "WorkbenchApp"):
        self.app = app
        self.list_col: ft.Column
        self.preview_col: ft.Column
        self.filter_field: ft.TextField
        self.body: ft.Control

    # --- build ---
    def build_body(self) -> ft.Control:
        self.list_col = ft.Column(
            spacing=2, scroll=ft.ScrollMode.AUTO, expand=True,
        )
        self.preview_col = ft.Column(
            spacing=12, scroll=ft.ScrollMode.AUTO, expand=True,
        )
        # Filter field — essential with 100+ name stubs migrated from Airtable
        self.filter_field = ft.TextField(
            hint_text="Filter people…",
            value=self.app.state.people_filter,
            prefix_icon=ft.Icons.SEARCH,
            dense=True,
            on_change=self.on_filter_change,
        )
        self.body = self.app._build_browser_view_body(
            self.list_col, self.preview_col,
            header_controls=[self.filter_field],
        )
        return self.body

    def build_sidebar_row(self) -> ft.Container:
        return self.app._build_browser_sidebar_row(
            kind="people", label="People",
            icon=ft.Icons.PEOPLE_ROUNDED,
            icon_color=ft.Colors.PURPLE_400,
            count=len(self.app.state.people),
            on_click=lambda e: self.open(),
        )

    # --- refresh ---
    def refresh(self):
        app = self.app
        app._refresh_browser_list(
            list_col=self.list_col,
            entries=self._filtered(),
            label="People",
            icon=ft.Icons.PEOPLE_OUTLINE,
            selected_path=app.state.selected_person_path,
            on_select=self.on_select,
            on_refresh=self.on_refresh,
            empty_hint=("No matches." if app.state.people_filter
                        else "No notes in vault/40_People/."),
            total_count=len(app.state.people),
        )
        app._refresh_browser_preview(
            preview_col=self.preview_col,
            entries=app.state.people,
            selected_path_attr="selected_person_path",
            on_close=self.on_close_preview,
            empty_msg=("No people yet.\n\n"
                       "Anything in vault/40_People/ shows here."),
            no_selection_msg="Select a person to preview it here.",
        )

    def _filtered(self) -> list[VaultEntry]:
        q = self.app.state.people_filter.strip().lower()
        if not q:
            return self.app.state.people
        return [p for p in self.app.state.people if q in p.name.lower()]

    # --- handlers ---
    def open(self):
        self.app._open_or_focus(OpenTab(kind="people", ref_id="people"))

    def on_select(self, path: str):
        self.app.state.selected_person_path = path
        self.refresh()
        self.app.page.update()

    def on_refresh(self):
        # Vault reload stays on the app (owns VAULT_PATH + the global refresh).
        self.app.reload_people()

    def on_close_preview(self):
        self.app.state.selected_person_path = None
        self.refresh()
        self.app.page.update()

    def on_filter_change(self, e):
        self.app.state.people_filter = e.control.value or ""
        # Filter only narrows the list; selection + preview stay as-is even if hidden.
        self.refresh()
        self.app.page.update()
