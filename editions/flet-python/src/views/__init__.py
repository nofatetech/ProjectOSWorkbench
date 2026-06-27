"""View components carved out of the WorkbenchApp god object.

Each view owns its own controls + view-logic and holds a reference to the app
(`self.app`) for shared services: the browser list/preview renderers, the
sidebar-row builder, navigation (`_open_or_focus`), the global `refresh()`, and
vault data reload (which stays on the app because VAULT_PATH is a main-module
global reassigned on vault switch).
"""
