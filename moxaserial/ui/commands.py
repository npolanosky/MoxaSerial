"""Toolbar command definitions for the Manufacture (CAM) workspace.

Three buttons live in a ``Moxa DNC`` panel on the shared ``P3DTools``
tab (the convention the sibling add-ins use), and "Send Last Program" is
*also* added to ``CAMActionPanel`` on the Milling and Turning tabs - the
panel that already holds Post Process - so the common case is one click
away from posting, without opening the palette at all.

* **Send Last Program** - resolve the newest posted NC file, send it to
  the active machine, report via toast. No dialog, no palette.
* **Open DNC Panel** - show the palette.
* **Receive** - arm a receive on the active machine.
"""

from __future__ import annotations

import os
from typing import Any

from moxaserial.log import get_logger

log = get_logger("commands")

CMD_SEND_LAST = "P3D_MoxaSerial_SendLast"
CMD_PANEL = "P3D_MoxaSerial_Panel"
CMD_RECEIVE = "P3D_MoxaSerial_Receive"

PANEL_ID = "P3D_MoxaSerialPanel"
PANEL_NAME = "Moxa DNC"
TAB_ID = "P3DToolsTab"
TAB_NAME = "P3DTools"
WORKSPACE_ID = "CAMEnvironment"

#: Tabs whose CAMActionPanel (where Post Process lives) also gets the
#: one-click send button.
ACTION_PANEL_TABS = ("MillingTab", "TurningTab")
ACTION_PANEL_ID = "CAMActionPanel"

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _icons(name: str) -> str:
    folder = os.path.join(_ROOT, "resources", name)
    return folder if os.path.isdir(folder) else ""


def create(ui: Any, handlers: list, callbacks: dict[str, Any]) -> None:
    """Build the command definitions and place them in the toolbars."""
    import adsk.core  # type: ignore

    class _Created(adsk.core.CommandCreatedEventHandler):
        """Fires immediately on click; we run and then cancel the command.

        These are *actions*, not dialogs, so the work happens in
        ``execute`` and the command terminates without showing any UI.
        """

        def __init__(self, fn):
            super().__init__()
            self._fn = fn

        def notify(self, args: Any) -> None:
            try:
                cmd = args.command
                cmd.isAutoExecute = True
                cmd.isExecutedWhenPreEmpted = False

                class _Execute(adsk.core.CommandEventHandler):
                    def __init__(self, fn):
                        super().__init__()
                        self._fn = fn

                    def notify(self, exec_args: Any) -> None:
                        try:
                            self._fn()
                        except Exception:
                            log.exception("Toolbar command failed")

                on_execute = _Execute(self._fn)
                cmd.execute.add(on_execute)
                handlers.append(on_execute)
            except Exception:
                log.exception("Command creation failed")

    definitions = (
        (
            CMD_SEND_LAST,
            "Send Last Program",
            "Send the most recently posted NC program to the active machine over RS-232.",
            "send",
            callbacks["send_last"],
        ),
        (
            CMD_PANEL,
            "DNC Panel",
            "Open the Moxa DNC panel: machines, send, receive and log.",
            "panel",
            callbacks["open_panel"],
        ),
        (
            CMD_RECEIVE,
            "Receive Program",
            "Wait for the control to punch a program out and save it to disk.",
            "receive",
            callbacks["receive"],
        ),
    )

    made = []
    for cmd_id, name, tooltip, icon, fn in definitions:
        defn = ui.commandDefinitions.itemById(cmd_id)
        if defn is not None:
            # Left by a previous add-in instance: its commandCreated handler
            # points at purged code, and adding ours would run both.
            try:
                defn.deleteMe()
            except Exception:
                log.debug("Could not delete stale command definition %s", cmd_id, exc_info=True)
            defn = ui.commandDefinitions.itemById(cmd_id)
        if defn is None:
            defn = ui.commandDefinitions.addButtonDefinition(cmd_id, name, tooltip, _icons(icon))
        created = _Created(fn)
        defn.commandCreated.add(created)
        handlers.append(created)
        made.append(defn)

    ws = ui.workspaces.itemById(WORKSPACE_ID)
    if ws is None:
        log.warning("Manufacture workspace '%s' not found - no toolbar buttons.", WORKSPACE_ID)
        return

    tab = ws.toolbarTabs.itemById(TAB_ID) or ws.toolbarTabs.add(TAB_ID, TAB_NAME)
    panel = tab.toolbarPanels.itemById(PANEL_ID)
    if panel is None:
        panel = tab.toolbarPanels.add(PANEL_ID, PANEL_NAME, "", False)
    for defn in made:
        if panel.controls.itemById(defn.id) is None:
            panel.controls.addCommand(defn)

    # Also drop the one-click send next to Post Process.
    send_def = ui.commandDefinitions.itemById(CMD_SEND_LAST)
    for tab_id in ACTION_PANEL_TABS:
        try:
            other_tab = ws.toolbarTabs.itemById(tab_id)
            if other_tab is None:
                continue
            action_panel = other_tab.toolbarPanels.itemById(ACTION_PANEL_ID)
            if action_panel is None:
                continue
            if action_panel.controls.itemById(CMD_SEND_LAST) is None:
                action_panel.controls.addCommand(send_def)
        except Exception:
            log.debug("Could not add to %s/%s", tab_id, ACTION_PANEL_ID, exc_info=True)

    log.info("Toolbar commands installed.")


def destroy(ui: Any) -> None:
    """Remove everything :func:`create` added."""
    ws = ui.workspaces.itemById(WORKSPACE_ID)
    if ws is not None:
        for tab_id in ACTION_PANEL_TABS:
            try:
                tab = ws.toolbarTabs.itemById(tab_id)
                panel = tab.toolbarPanels.itemById(ACTION_PANEL_ID) if tab else None
                ctrl = panel.controls.itemById(CMD_SEND_LAST) if panel else None
                if ctrl:
                    ctrl.deleteMe()
            except Exception:
                pass
        try:
            tab = ws.toolbarTabs.itemById(TAB_ID)
            if tab is not None:
                panel = tab.toolbarPanels.itemById(PANEL_ID)
                if panel is not None:
                    for i in range(panel.controls.count - 1, -1, -1):
                        panel.controls.item(i).deleteMe()
                    panel.deleteMe()
                # Leave the shared tab alone unless we were its last tenant.
                if tab.toolbarPanels.count == 0:
                    tab.deleteMe()
        except Exception:
            log.debug("Panel teardown failed", exc_info=True)

    for cmd_id in (CMD_SEND_LAST, CMD_PANEL, CMD_RECEIVE):
        try:
            defn = ui.commandDefinitions.itemById(cmd_id)
            if defn:
                defn.deleteMe()
        except Exception:
            pass
