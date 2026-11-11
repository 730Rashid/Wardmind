"""Geometry for the onboarding runway: one bar per account, with the day it has reached, the end of
its window and the day it is projected to finish, all as percentages of the bar's width.

Pure functions only. The template places the marks and writes the words; this module does the
arithmetic, so every edge case (no start date, a day past the window, a finish past the window, a
zero-length window) is checked here rather than in the markup.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Runway:
    """The marks on one runway. Every position is a percentage of the full width, 0 to 100, to one
    decimal place. The full width stands for `scale_max` days."""

    scale_max: int  # the largest of the window, today and the finish; 0 when all are unknown
    elapsed_pct: float  # from the start to today; 0 when the start date isn't known
    window_end_pct: float  # where the window ends
    projected_pct: float | None  # where the projected finish sits; None when there is none
    overrun_pct: float  # from the window's end to the projected finish when that is late; else 0
    late: bool  # the projected finish is after the window


def runway(day: int | None, window_days: int, projected_day: int | None) -> Runway:
    """The runway for one onboarding account.

    `day` is the account's day in its window (1 is the start date), or None when the start date
    isn't known. `projected_day` is the forecast's finish, or None when there is no forecast. A
    negative window is treated as zero. With nothing known at all the scale is 0 and every mark
    sits at 0, so the caller never divides by zero.
    """
    window = max(window_days, 0)
    today = None if day is None else max(day, 0)
    projected = None if projected_day is None else max(projected_day, 0)
    scale = max(window, today or 0, projected or 0)
    if scale == 0:
        return Runway(
            scale_max=0,
            elapsed_pct=0.0,
            window_end_pct=0.0,
            projected_pct=None,
            overrun_pct=0.0,
            late=False,
        )

    def share(days: int) -> float:
        return round(days / scale * 100, 1)

    late = projected is not None and projected > window
    window_end = share(window)
    projected_pct = None if projected is None else share(projected)
    overrun = round(projected_pct - window_end, 1) if late and projected_pct is not None else 0.0
    return Runway(
        scale_max=scale,
        elapsed_pct=0.0 if today is None else share(today),
        window_end_pct=window_end,
        projected_pct=projected_pct,
        overrun_pct=overrun,
        late=late,
    )
