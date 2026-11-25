#!/usr/bin/env python3
"""Trend comparison charts for the customer health report.

Draws current-vs-previous window overlays from the downsampled series retained in
evidence (``retain_series`` recipes). Pure data → PNG; nothing is screenshotted.
Style follows the reference weekly-inspection spec §3.7: current window blue,
previous window orange/yellow, each window's mean as a dashed line, the peak point
annotated, an MM-DD x-axis aligned by day-into-window, and a log y-axis for P999.

matplotlib is an OPTIONAL dependency: ``charts_available()`` is False when it is
not installed, and the inspection pipeline then simply omits charts (never crashes).
Sizing: 12x4 in at 100 dpi, matching the reference.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # optional dependency — the core pipeline must run without it
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter
    import matplotlib.dates as mdates
    import matplotlib.font_manager as _fm
    _MPL = True
except Exception:  # pragma: no cover - exercised only when matplotlib is absent
    _MPL = False


# CJK-capable font so Chinese chart titles/legends render (not tofu boxes).
_CJK_FONTS = ["PingFang SC", "Arial Unicode MS", "Hiragino Sans GB", "Heiti SC",
              "STHeiti", "Songti SC", "Noto Sans CJK SC", "Microsoft YaHei"]


def _configure_cjk_font() -> None:
    if not _MPL:
        return
    available = {f.name for f in _fm.fontManager.ttflist}
    picked = next((name for name in _CJK_FONTS if name in available), None)
    if picked:
        plt.rcParams["font.sans-serif"] = [picked] + plt.rcParams.get("font.sans-serif", [])
        plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["axes.unicode_minus"] = False  # render minus signs with CJK font


if _MPL:
    _configure_cjk_font()


def charts_available() -> bool:
    return _MPL


# Metric list is shared with the renderer so refs and PNGs stay in lock-step.
from customer_report import CHART_METRICS  # noqa: E402  (key, cid, title, scale, log_y)

_COLOR_CUR = "#1f77b4"   # current window — blue
_COLOR_PREV = "#ff9f1c"  # previous window — orange/yellow


def _epoch(iso: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _series(result: dict[str, Any] | None, scale: float) -> list[tuple[float, float]]:
    """[(epoch, scaled value)] from an evidence 'series' ([[ts, v], ...])."""
    ev = (result or {}).get("evidence") or {}
    raw = ev.get("series")
    out: list[tuple[float, float]] = []
    if isinstance(raw, list):
        for point in raw:
            try:
                out.append((float(point[0]), float(point[1]) * scale))
            except (IndexError, TypeError, ValueError):
                continue
    return out




def generate_charts(payload: dict[str, Any], charts_dir: str | Path, lang: str = "zh") -> dict[str, str]:
    """Write one PNG per chartable metric that has data into ``charts_dir``.
    Returns {metric_key: relative_path} (``charts/<key>.png``) for those written.
    No-op (returns {}) when matplotlib is unavailable."""
    if not _MPL:
        return {}
    cur = {str(r.get("check_id")): r for r in payload.get("check_results", []) if isinstance(r, dict)}
    prev = payload.get("previous_period") or {}
    cur_start = _epoch((payload.get("window") or {}).get("start"))
    prev_start = _epoch((payload.get("previous_window") or {}).get("start"))
    cur_range = _date_range((payload.get("window") or {}).get("start"), (payload.get("window") or {}).get("end"))
    prev_range = _date_range((payload.get("previous_window") or {}).get("start"), (payload.get("previous_window") or {}).get("end"))
    charts_dir = Path(charts_dir)
    produced: dict[str, str] = {}

    for key, cid, title, scale, log_y in CHART_METRICS:
        cur_series = _series(cur.get(cid), scale)
        if not cur_series:
            continue  # metric unavailable this window → no chart
        prev_series = _series(prev.get(cid), scale)
        charts_dir.mkdir(parents=True, exist_ok=True)
        path = charts_dir / f"{key}.png"
        _draw_one(path, title.get(lang) or title["en"], cur_series, prev_series,
                  cur_range, prev_range, log_y, lang)
        produced[key] = f"charts/{key}.png"
    return produced


def _date_range(start_iso: Any, end_iso: Any) -> str:
    """"MM-DD ~ MM-DD" for a window (UTC); '' when unknown."""
    s, e = _epoch(start_iso), _epoch(end_iso)
    if s is None:
        return ""
    sd = datetime.fromtimestamp(s, timezone.utc).strftime("%m-%d")
    ed = datetime.fromtimestamp(e, timezone.utc).strftime("%m-%d") if e is not None else "?"
    return f"{sd}~{ed}"


def _panel(ax, series, color, subtitle: str, log_y: bool) -> None:
    """One period's line on its own real-time x-axis (MM-DD), with a mean dashed
    line and the peak annotated."""
    xs = [datetime.utcfromtimestamp(ts) for ts, _ in series]
    ys = [v for _, v in series]
    ax.plot(xs, ys, color=color, linewidth=1.2)
    mean = sum(ys) / len(ys)
    ax.axhline(mean, color=color, linestyle="--", linewidth=0.9, alpha=0.75)
    pk = max(range(len(ys)), key=lambda i: ys[i])
    ax.plot([xs[pk]], [ys[pk]], marker="o", color=color, markersize=4)
    ax.annotate(f"max {ys[pk]:,.1f}", xy=(xs[pk], ys[pk]), xytext=(0, 8),
                textcoords="offset points", fontsize=8, color=color, ha="center")
    if log_y:
        ax.set_yscale("log")
    ax.set_title(subtitle, fontsize=10)
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _pos: f"{v:,.0f}" if v >= 1 else f"{v:g}"))
    ax.grid(True, linestyle=":", linewidth=0.5, alpha=0.5)
    ax.tick_params(axis="x", labelsize=8)


def _draw_one(path: Path, title: str, cur_series, prev_series,
              cur_range: str, prev_range: str, log_y: bool, lang: str) -> None:
    # One figure split into two real-time panels: left = previous period, right =
    # current period, each on its own date axis; a shared y-axis keeps them comparable.
    cur_name = "本周期" if lang == "zh" else "Current"
    prev_name = "上周期" if lang == "zh" else "Previous"
    if prev_series:
        fig, (axL, axR) = plt.subplots(1, 2, figsize=(12, 4), dpi=100, sharey=True)
        _panel(axL, prev_series, _COLOR_PREV, f"{prev_name}  {prev_range}", log_y)
        _panel(axR, cur_series, _COLOR_CUR, f"{cur_name}  {cur_range}", log_y)
        fig.suptitle(title, fontsize=12, y=1.02)
    else:
        fig, ax = plt.subplots(figsize=(12, 4), dpi=100)
        _panel(ax, cur_series, _COLOR_CUR, f"{cur_name}  {cur_range}", log_y)
        fig.suptitle(title, fontsize=12, y=1.02)
    fig.autofmt_xdate(rotation=0)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
