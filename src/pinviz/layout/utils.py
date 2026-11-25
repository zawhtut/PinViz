"""Utility functions for layout calculations."""

import math

from ..model import Point


def create_orthogonal_path(points: list[Point], corner_radius: float = 5.0) -> str:
    """
    Create an SVG path string with grid-aligned orthogonal segments and rounded corners.

    Emits explicit horizontal (`H`) and vertical (`V`) commands between axis-aligned
    waypoints, rounding 90-degree turns with quadratic Bezier (`Q`) arcs up to
    ``corner_radius`` (clamped to half the adjacent segment lengths).

    Args:
        points: Ordered list of orthogonal waypoints (at least 2 points)
        corner_radius: Radius for rounding 90-degree corners

    Returns:
        SVG path `d` attribute string
    """
    if len(points) < 2:
        return ""

    def _seg_cmd(start: Point, end: Point) -> str:
        if abs(end.y - start.y) < 1e-6:
            return f"H {end.x:.2f}"
        if abs(end.x - start.x) < 1e-6:
            return f"V {end.y:.2f}"
        return f"L {end.x:.2f},{end.y:.2f}"

    path_parts = [f"M {points[0].x:.2f},{points[0].y:.2f}"]
    pen = points[0]

    for i in range(1, len(points) - 1):
        prev = points[i - 1]
        curr = points[i]
        next_pt = points[i + 1]

        dx1, dy1 = curr.x - prev.x, curr.y - prev.y
        dx2, dy2 = next_pt.x - curr.x, next_pt.y - curr.y
        len1 = math.hypot(dx1, dy1)
        len2 = math.hypot(dx2, dy2)

        if len1 < 1e-6 or len2 < 1e-6:
            continue

        # Skip corner rounding on collinear segments
        cross = abs(dx1 * dy2 - dy1 * dx2)
        if cross < 1e-6 or corner_radius <= 0:
            path_parts.append(_seg_cmd(pen, curr))
            pen = curr
            continue

        radius = min(corner_radius, len1 / 2.0, len2 / 2.0)
        if radius < 0.5:
            path_parts.append(_seg_cmd(pen, curr))
            pen = curr
            continue

        ux1, uy1 = dx1 / len1, dy1 / len1
        ux2, uy2 = dx2 / len2, dy2 / len2

        entry = Point(curr.x - ux1 * radius, curr.y - uy1 * radius)
        exit_pt = Point(curr.x + ux2 * radius, curr.y + uy2 * radius)

        if math.hypot(entry.x - pen.x, entry.y - pen.y) >= 1e-6:
            path_parts.append(_seg_cmd(pen, entry))
        path_parts.append(f"Q {curr.x:.2f},{curr.y:.2f} {exit_pt.x:.2f},{exit_pt.y:.2f}")
        pen = exit_pt

    last = points[-1]
    if math.hypot(last.x - pen.x, last.y - pen.y) >= 1e-6:
        path_parts.append(_seg_cmd(pen, last))

    return " ".join(path_parts)


def create_bezier_path(points: list[Point], corner_radius: float = 5.0) -> str:
    """
    Create an SVG path string with smooth Bezier curves.

    Creates organic, flowing curves through the points using cubic Bezier curves,
    similar to the classic Fritzing diagram style.

    Args:
        points: List of points defining the path (including control points)
        corner_radius: Not used, kept for API compatibility

    Returns:
        SVG path d attribute string with smooth curves
    """
    if len(points) < 2:
        return ""

    # Start at first point
    path_parts = [f"M {points[0].x:.2f},{points[0].y:.2f}"]

    if len(points) == 2:
        # Simple line
        path_parts.append(f"L {points[1].x:.2f},{points[1].y:.2f}")
    elif len(points) == 3:
        # Quadratic Bezier through middle point
        path_parts.append(
            f"Q {points[1].x:.2f},{points[1].y:.2f} {points[2].x:.2f},{points[2].y:.2f}"
        )
    elif len(points) == 4:
        # Smooth cubic Bezier using middle two points as control points
        path_parts.append(
            f"C {points[1].x:.2f},{points[1].y:.2f} "
            f"{points[2].x:.2f},{points[2].y:.2f} "
            f"{points[3].x:.2f},{points[3].y:.2f}"
        )
    elif len(points) == 5:
        # Cubic Bezier curve followed by straight line into pin
        # This ensures the wire visually connects directly into the device pin
        # points[0] = start, points[1] = ctrl1, points[2] = ctrl2
        # points[3] = connection point, points[4] = pin center

        # Smooth cubic Bezier using middle two points as control points
        path_parts.append(
            f"C {points[1].x:.2f},{points[1].y:.2f} "
            f"{points[2].x:.2f},{points[2].y:.2f} "
            f"{points[3].x:.2f},{points[3].y:.2f}"
        )

        # Straight line segment into the pin for clear visual connection
        path_parts.append(f"L {points[4].x:.2f},{points[4].y:.2f}")
    else:
        # Many points - create smooth curve through all
        i = 1
        while i < len(points):
            if i == len(points) - 1:
                # Last segment - simple curve
                prev = points[i - 1]
                curr = points[i]
                # Create smooth approach to final point
                cx = prev.x + (curr.x - prev.x) * 0.5
                path_parts.append(f"Q {cx:.2f},{curr.y:.2f} {curr.x:.2f},{curr.y:.2f}")
                i += 1
            else:
                # Use current point as control, next as target
                curr = points[i]
                next_pt = points[i + 1]
                path_parts.append(f"Q {curr.x:.2f},{curr.y:.2f} {next_pt.x:.2f},{next_pt.y:.2f}")
                i += 2  # Skip next point since we used it as target

    return " ".join(path_parts)
