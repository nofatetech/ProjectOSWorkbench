"""Platinum 9 look & feel — palette tokens + bevel/sticky primitives.

See _System/Methods/Workbench UI.md. Grayscale 3D bevels, 2px corners, one
classic-blue accent. Light source is ALWAYS top-left: raised = highlight on
top/left, shadow bottom/right; recessed = inverse. The only drop shadow in the
app is the sticky (loose paper on the desk; chrome uses bevels, not shadows).
"""

from typing import Optional

import flet as ft


STATUS_COLORS = {
    "idea": ft.Colors.OUTLINE,
    "active": ft.Colors.PRIMARY,
    "persist": ft.Colors.TERTIARY,
    "pause": ft.Colors.SECONDARY,
    "pivot": ft.Colors.ON_SECONDARY_CONTAINER,
    "done": ft.Colors.OUTLINE_VARIANT,
}


def _all_border(color=ft.Colors.OUTLINE_VARIANT, width: int = 1) -> ft.Border:
    side = ft.BorderSide(width=width, color=color)
    return ft.Border(top=side, right=side, bottom=side, left=side)


# --- Platinum 9 look & feel (see _System/Methods/Workbench UI.md) ---
# Grayscale 3D bevels, 2px corners, one classic-blue accent. Light source is
# ALWAYS top-left: raised = hi on top/left, lo on bottom/right; recessed = inverse.
PLATINUM = {
    "canvas": "#CCCCCC",    # window backdrop (sidebar + page bg)
    "face": "#DDDDDD",      # default control / inactive tab face
    "face_hover": "#E6E6E6",
    "panel": "#EEEEEE",     # content area + active tab fill
    "hi_bevel": "#FFFFFF",  # top/left highlight edge
    "lo_bevel": "#888888",  # bottom/right shadow edge
    "outline": "#555555",   # 1px outer frame (emphasis only)
    "text": "#1A1A1A",      # primary
    "text2": "#5A5A5A",     # secondary / disabled
    "accent": "#3366CC",    # selection highlight (classic blue)
    "accent_txt": "#FFFFFF",
}


def _bevel(raised: bool = True) -> ft.Border:
    """A 1px two-tone Platinum bevel. raised=True → highlight top/left, shadow
    bottom/right (pops out); raised=False → inverse (a sunken well)."""
    hi = ft.BorderSide(1, PLATINUM["hi_bevel"])
    lo = ft.BorderSide(1, PLATINUM["lo_bevel"])
    if raised:
        return ft.Border(top=hi, left=hi, bottom=lo, right=lo)
    return ft.Border(top=lo, left=lo, bottom=hi, right=hi)


def _raised(content, fill: str = PLATINUM["face"], radius: int = 2) -> ft.Container:
    return ft.Container(content=content, bgcolor=fill, border_radius=radius,
                        border=_bevel(raised=True))


def _recessed(content, fill: str = PLATINUM["panel"], radius: int = 2) -> ft.Container:
    return ft.Container(content=content, bgcolor=fill, border_radius=radius,
                        border=_bevel(raised=False))


# Sticky / loose note (board views only — see _System/Methods/Workbench UI.md).
# chrome = bevel, loose notes = drop shadow. The shadow still obeys the top-left
# light source (falls bottom-right). This is the ONLY shadow in the app.
STICKY_YELLOW = "#FFFFCC"
STICKY_BORDER = "#D6D6A0"  # a darker shade of the fill — paper, not gray chrome


def _sticky(content, fill: str = STICKY_YELLOW, width: Optional[int] = None) -> ft.Container:
    return ft.Container(
        content=content, bgcolor=fill, width=width,
        padding=ft.Padding(left=10, top=8, right=10, bottom=10),
        border_radius=2,
        border=_all_border(STICKY_BORDER, 1),
        shadow=ft.BoxShadow(blur_radius=4, color="#38000000",
                            offset=ft.Offset(2, 2)),
    )


def _text_on(bg_hex: str, opacity: float = 1.0) -> str:
    """Pick a high-contrast text color for the given bg. Accepts '#RRGGBB' or '#AARRGGBB'.
    Returns '#RRGGBB' or '#AARRGGBB' (when opacity < 1)."""
    h = bg_hex.lstrip("#")
    if len(h) == 8:
        h = h[2:]  # drop alpha for luminance calc
    light = (255, 255, 255)  # white (on dark fills)
    dark = (26, 26, 26)      # near-black (on light fills) — Platinum text
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        rgb = light
    else:
        lum = (r * 299 + g * 587 + b * 114) / 1000
        rgb = dark if lum > 140 else light
    if opacity < 1.0:
        a = max(0, min(255, int(opacity * 255)))
        return f"#{a:02X}{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
