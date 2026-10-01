"""Coordinate spaces.

Three distinct spaces exist in every computer-use loop and mixing them up is one
of the most common silent failures:

  logical points  - what the OS input layer accepts (1440x900 on this project)
  screen pixels   - what a Retina screenshot actually contains (2880x1800)
  model image     - what the model was shown, after downscale/letterbox

Models also disagree on how they express a point: absolute pixels of the image
they saw (Holo), 0-1000 normalised (UI-TARS), or 0-1 normalised. The adapter
declares its convention; this module is the only place that converts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class Size:
    w: int
    h: int

    def as_tuple(self) -> tuple[int, int]:
        return (self.w, self.h)


@dataclass(frozen=True)
class Point:
    x: float
    y: float

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)

    def rounded(self) -> tuple[int, int]:
        return (round(self.x), round(self.y))


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def center(self) -> Point:
        return Point(self.x + self.w / 2, self.y + self.h / 2)

    def contains(self, p: Point) -> bool:
        return self.x <= p.x < self.x + self.w and self.y <= p.y < self.y + self.h

    def on_screen(self, origin: Point) -> list[float]:
        """[x, y, w, h] in screen points (top-left origin) of a rect local to a window whose top-left is `origin`:
        what a recording of the screen shows, for drawing on it afterwards."""
        return [round(origin.x + self.x, 1), round(origin.y + self.y, 1), round(self.w, 1), round(self.h, 1)]


class CoordSpace(str, Enum):
    """How a model expresses a coordinate in its output."""

    MODEL_IMAGE = "model_image"      # absolute pixels of the image the model saw
    NORM_1000 = "norm_1000"          # 0..1000 on each axis (UI-TARS convention)
    NORM_UNIT = "norm_unit"          # 0..1 on each axis
    SCREEN_PIXELS = "screen_pixels"  # raw screenshot pixels
    LOGICAL_POINTS = "logical_points"


@dataclass(frozen=True)
class ScreenGeometry:
    """Relates the screenshot the driver produced to the input coordinate space."""

    logical: Size
    pixels: Size

    def __post_init__(self) -> None:
        if self.logical.w <= 0 or self.logical.h <= 0:
            raise ValueError("logical size must be positive")
        if self.pixels.w <= 0 or self.pixels.h <= 0:
            raise ValueError("pixel size must be positive")

    @property
    def backing_scale_x(self) -> float:
        return self.pixels.w / self.logical.w

    @property
    def backing_scale_y(self) -> float:
        return self.pixels.h / self.logical.h


@dataclass(frozen=True)
class ImageTransform:
    """How screen pixels were turned into the image handed to the model.

    ``pad`` is the letterbox offset inside the target image, so a point at the
    very top-left of the *content* sits at ``pad`` in model image space.
    """

    source: Size
    target: Size
    pad_x: int = 0
    pad_y: int = 0

    @classmethod
    def identity(cls, size: Size) -> "ImageTransform":
        return cls(source=size, target=size)

    @classmethod
    def fit(cls, source: Size, max_side: int, min_side: int = 0) -> "ImageTransform":
        """Scale so the longest side lands between ``min_side`` and ``max_side``.

        Upscaling matters as much as downscaling. A 460x218 capture of a small
        Finder window was sent through unchanged, and the model answered (400,
        300) on a 218-tall image -- a round number in a frame it had imagined,
        because the real one was too small to ground on. Every run in that batch
        died on the coordinate check. Enlarging costs tokens and nothing else.
        """
        longest = max(source.w, source.h)
        ratio = 1.0
        if longest > max_side:
            ratio = max_side / longest
        elif min_side and longest < min_side:
            ratio = min_side / longest
        if ratio == 1.0:
            return cls.identity(source)
        return cls(
            source=source,
            target=Size(max(1, round(source.w * ratio)), max(1, round(source.h * ratio))),
        )

    @property
    def content(self) -> Size:
        return Size(self.target.w - 2 * self.pad_x, self.target.h - 2 * self.pad_y)

    @property
    def scale_x(self) -> float:
        return self.content.w / self.source.w

    @property
    def scale_y(self) -> float:
        return self.content.h / self.source.h


class CoordinateError(ValueError):
    """A model produced a point that cannot be mapped onto the screen."""


def to_logical(
    point: Point,
    space: CoordSpace,
    geometry: ScreenGeometry,
    transform: ImageTransform | None = None,
) -> Point:
    """Convert a model-emitted point into logical points for the input layer."""
    if space is CoordSpace.LOGICAL_POINTS:
        return point

    if space is CoordSpace.SCREEN_PIXELS:
        px = point
    else:
        if transform is None:
            transform = ImageTransform.identity(geometry.pixels)
        if space is CoordSpace.NORM_1000:
            model_pt = Point(point.x / 1000.0 * transform.target.w,
                             point.y / 1000.0 * transform.target.h)
        elif space is CoordSpace.NORM_UNIT:
            model_pt = Point(point.x * transform.target.w,
                             point.y * transform.target.h)
        else:
            model_pt = point
        px = Point(
            (model_pt.x - transform.pad_x) / transform.scale_x,
            (model_pt.y - transform.pad_y) / transform.scale_y,
        )

    return Point(px.x / geometry.backing_scale_x, px.y / geometry.backing_scale_y)


#: How far outside the frame a point may sit and still be treated as an aiming
#: error rather than a coordinate-space error. Two pixels was too strict: a model
#: aiming at the bottom edge of a 352-point window answered 364.8 -- twelve
#: points over, plainly the right target -- and the run died with an exception
#: instead of clicking. The number that matters is the one that separates "aimed
#: slightly wide" from "using the wrong units", and 5% of the frame does that
#: while still catching a 2x or 1000-unit mistake.
EDGE_TOLERANCE_FRACTION = 0.05


def clamp_to_screen(point: Point, geometry: ScreenGeometry,
                    *, tolerance: float | None = None) -> Point:
    """Clamp a near-edge point, but reject one that is genuinely off-screen.

    A model that is off by a few points at the edge should still click; a model
    that returns 3000,2000 for a 1440x900 screen has a coordinate-space bug that
    must surface as a failure rather than a silent click in the corner.
    """
    if tolerance is None:
        tolerance = max(8.0, EDGE_TOLERANCE_FRACTION * max(geometry.logical.w,
                                                           geometry.logical.h))
    max_x = geometry.logical.w - 1
    max_y = geometry.logical.h - 1
    if point.x < -tolerance or point.y < -tolerance:
        raise CoordinateError(f"point {point} is off-screen (negative beyond tolerance)")
    if point.x > max_x + tolerance or point.y > max_y + tolerance:
        raise CoordinateError(
            f"point {point} is outside logical screen {geometry.logical.as_tuple()}"
        )
    return Point(min(max(point.x, 0.0), max_x), min(max(point.y, 0.0), max_y))
