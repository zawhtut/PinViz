"""Wire routing logic for diagram connections."""

import logging
import math
import time
from dataclasses import dataclass

from ..model import DEFAULT_COLORS, Diagram, Point, WireStyle
from .types import LayoutConfig, LayoutConstants, RoutedWire, WireData

logger = logging.getLogger(__name__)


class WireDataCollector:
    """Collects and groups wire data from diagram connections."""

    def __init__(
        self, board_margin_left: float, board_margin_top: float, constants: LayoutConstants
    ):
        self.board_margin_left = board_margin_left
        self.board_margin_top = board_margin_top
        self.constants = constants

    def collect(self, diagram: Diagram) -> list[WireData]:
        """Collect wire connection data from the diagram.

        Resolves absolute pin positions for both board-to-device and
        device-to-device connections.
        """
        wire_data: list[WireData] = []
        device_by_name = {device.name: device for device in diagram.devices}

        for conn in diagram.connections:
            is_device_to_device = conn.source_device is not None
            target_device = device_by_name.get(conn.device_name)
            if not target_device:
                continue
            target_pin = target_device.get_pin_by_name(conn.device_pin_name)
            if not target_pin:
                continue

            if is_device_to_device:
                source_device = device_by_name.get(conn.source_device)
                if not source_device:
                    continue
                source_pin = source_device.get_pin_by_name(conn.source_pin)
                if not source_pin:
                    continue

                from_pos = Point(
                    source_device.position.x + source_pin.position.x,
                    source_device.position.y + source_pin.position.y,
                )
                to_pos = Point(
                    target_device.position.x + target_pin.position.x,
                    target_device.position.y + target_pin.position.y,
                )
                is_source_right_side = source_pin.position.x > (source_device.width / 2)
                is_target_right_side = target_pin.position.x > (target_device.width / 2)

                if conn.color:
                    color = conn.color.value if hasattr(conn.color, "value") else conn.color
                else:
                    color = DEFAULT_COLORS.get(source_pin.role, "#808080")

                wire_data.append(
                    WireData(
                        conn,
                        from_pos,
                        to_pos,
                        color,
                        target_device,
                        source_device,
                        is_source_right_side,
                        is_target_right_side,
                    )
                )
            else:
                board_pin = diagram.board.get_pin_by_number(conn.board_pin)
                if not board_pin or not board_pin.position:
                    continue

                from_pos = Point(
                    self.board_margin_left + board_pin.position.x,
                    self.board_margin_top + board_pin.position.y,
                )
                to_pos = Point(
                    target_device.position.x + target_pin.position.x,
                    target_device.position.y + target_pin.position.y,
                )
                is_target_right_side = target_pin.position.x > (target_device.width / 2)

                if conn.color:
                    color = conn.color.value if hasattr(conn.color, "value") else conn.color
                else:
                    color = DEFAULT_COLORS.get(board_pin.role, "#808080")

                wire_data.append(
                    WireData(
                        conn,
                        from_pos,
                        to_pos,
                        color,
                        target_device,
                        None,
                        False,
                        is_target_right_side,
                    )
                )

        return wire_data

    def group_by_position(self, wire_data: list[WireData]) -> dict[int, list[int]]:
        """Group wires by starting Y position for vertical offset calculation."""
        y_tolerance = self.constants.Y_POSITION_TOLERANCE
        y_groups: dict[int, list[int]] = {}

        for idx, wire in enumerate(wire_data):
            group_id = None
            for gid, wire_indices in y_groups.items():
                first_wire_y = wire_data[wire_indices[0]].from_pos.y
                if abs(wire.from_pos.y - first_wire_y) < y_tolerance:
                    group_id = gid
                    break

            if group_id is None:
                group_id = len(y_groups)
                y_groups[group_id] = []

            y_groups[group_id].append(idx)

        return y_groups


class RailAssigner:
    """Assigns vertical rail positions to devices for wire routing."""

    def __init__(self, config: LayoutConfig, constants: LayoutConstants, board_margin_left: float):
        self.config = config
        self.constants = constants
        self.board_margin_left = board_margin_left

    def assign(
        self,
        wire_data: list[WireData],
        board_width: float,
        y_groups: dict[int, list[int]],
    ) -> list[dict]:
        """Assign rail positions and calculate initial wire paths.

        Returns list of wire info dicts with rail_x, y_offset, and positions.
        """
        device_to_base_rail, wire_count_per_device = self._assign_rail_positions(
            wire_data, board_width
        )
        return self._calculate_initial_wire_paths(
            wire_data, y_groups, device_to_base_rail, wire_count_per_device
        )

    def _assign_rail_positions(
        self, wire_data: list[WireData], board_width: float
    ) -> tuple[dict[str, float], dict[str, int]]:
        """Assign rail X positions for each device to prevent wire crossings."""
        board_right_edge = self.board_margin_left + board_width
        base_rail_x = board_right_edge + self.config.rail_offset

        unique_devices = []
        seen_devices: set[str] = set()
        for wire in wire_data:
            if wire.device.name not in seen_devices:
                unique_devices.append(wire.device)
                seen_devices.add(wire.device.name)

        device_to_base_rail: dict[str, float] = {}
        for idx, device in enumerate(unique_devices):
            device_to_base_rail[device.name] = base_rail_x + (
                idx * self.config.wire_spacing * self.constants.RAIL_SPACING_MULTIPLIER
            )

        wire_count_per_device: dict[str, int] = {}
        for wire in wire_data:
            wire_count_per_device[wire.device.name] = (
                wire_count_per_device.get(wire.device.name, 0) + 1
            )

        return device_to_base_rail, wire_count_per_device

    def _calculate_initial_wire_paths(
        self,
        wire_data: list[WireData],
        y_groups: dict[int, list[int]],
        device_to_base_rail: dict[str, float],
        wire_count_per_device: dict[str, int],
    ) -> list[dict]:
        """Calculate initial wire paths with rail positions and vertical offsets."""
        wire_index_per_device: dict[str, int] = {}
        initial_wires = []

        for wire_idx, wire in enumerate(wire_data):
            base_rail = device_to_base_rail[wire.device.name]
            dev_wire_idx = wire_index_per_device.get(wire.device.name, 0)
            wire_index_per_device[wire.device.name] = dev_wire_idx + 1

            num_wires = wire_count_per_device[wire.device.name]
            if num_wires > 1:
                spread = (num_wires - 1) * self.config.wire_spacing / 2
                offset = dev_wire_idx * self.config.wire_spacing - spread
            else:
                offset = 0

            rail_x = base_rail + offset

            y_offset = 0.0
            for _group_id, group_indices in y_groups.items():
                if wire_idx in group_indices:
                    pos_in_group = group_indices.index(wire_idx)
                    num_in_group = len(group_indices)
                    if num_in_group > 1:
                        vertical_spacing = (
                            self.config.wire_spacing * self.constants.VERTICAL_SPACING_MULTIPLIER
                        )
                        spread = (num_in_group - 1) * vertical_spacing / 2
                        y_offset = pos_in_group * vertical_spacing - spread
                    break

            initial_wires.append(
                {
                    "conn": wire.connection,
                    "from_pos": wire.from_pos,
                    "to_pos": wire.to_pos,
                    "color": wire.color,
                    "device": wire.device,
                    "rail_x": rail_x,
                    "y_offset": y_offset,
                    "wire_idx": wire_idx,
                    "source_device": wire.source_device,
                    "is_source_right_side": wire.is_source_right_side,
                    "is_target_right_side": wire.is_target_right_side,
                }
            )

        return initial_wires


class PathGenerator:
    """Generates Bezier curve paths for wire routing."""

    def __init__(self, constants: LayoutConstants):
        self.constants = constants

    def generate(
        self, initial_wires: list[dict], y_offset_adjustments: dict[int, float]
    ) -> list[RoutedWire]:
        """Generate final routed wires with all adjustments applied."""
        routed_wires: list[RoutedWire] = []

        for wire_info in initial_wires:
            adjustment = y_offset_adjustments.get(wire_info["wire_idx"], 0.0)
            final_y_offset = wire_info["y_offset"] + adjustment

            path_points = self.calculate_path(
                wire_info["from_pos"],
                wire_info["to_pos"],
                wire_info["rail_x"],
                final_y_offset,
                wire_info["conn"].style,
                wire_info["is_source_right_side"],
                wire_info["is_target_right_side"],
            )

            routed_wires.append(
                RoutedWire(
                    connection=wire_info["conn"],
                    path_points=path_points,
                    color=wire_info["color"],
                    from_pin_pos=wire_info["from_pos"],
                    to_pin_pos=wire_info["to_pos"],
                )
            )

        return routed_wires

    def calculate_path(
        self,
        from_pos: Point,
        to_pos: Point,
        rail_x: float,
        y_offset: float,
        style: WireStyle,
        is_source_right_side: bool = False,
        is_target_right_side: bool = False,
    ) -> list[Point]:
        """Calculate wire path selecting the appropriate curve type."""
        connection_point, extended_end = self._connection_points(to_pos, is_target_right_side)

        if is_source_right_side:
            from_pos = Point(from_pos.x + self.constants.WIRE_PIN_EXTENSION, from_pos.y)
            if to_pos.x > from_pos.x:
                return self._align_exit_tangent(
                    self._right_to_right_path(from_pos, connection_point, extended_end, y_offset)
                )

        dy = to_pos.y - from_pos.y

        if abs(dy) < self.constants.SIMILAR_Y_THRESHOLD:
            return self._align_exit_tangent(
                self._gentle_arc_path(from_pos, rail_x, y_offset, connection_point, extended_end)
            )
        else:
            return self._align_exit_tangent(
                self._s_curve_path(from_pos, rail_x, y_offset, connection_point, extended_end)
            )

    def _align_exit_tangent(self, path_points: list[Point]) -> list[Point]:
        """
        Make the curve's second control point collinear with the straight lead-in
        into the pin (connection_point -> pin), so the Bezier meets that segment
        tangent-continuously instead of at an angle (which shows up as a kink at
        the device pin). Only applies to the 5-point curve+lead-in form.
        """
        if len(path_points) != 5:
            return path_points
        p0, _ctrl1, _ctrl2, cp, pin = path_points
        ldx, ldy = pin.x - cp.x, pin.y - cp.y
        lead_len = (ldx * ldx + ldy * ldy) ** 0.5
        if lead_len <= 1e-6:
            return path_points
        ux, uy = ldx / lead_len, ldy / lead_len
        span = ((cp.x - p0.x) ** 2 + (cp.y - p0.y) ** 2) ** 0.5
        back = max(lead_len * 4.0, span * 0.30)
        path_points[2] = Point(cp.x - ux * back, cp.y - uy * back)
        return path_points

    def _connection_points(self, to_pos: Point, is_right_side: bool = False) -> tuple[Point, Point]:
        """Calculate connection and extended end points for wire routing."""
        if is_right_side:
            connection_point = Point(to_pos.x + self.constants.STRAIGHT_SEGMENT_LENGTH, to_pos.y)
            extended_end = Point(to_pos.x - self.constants.WIRE_PIN_EXTENSION, to_pos.y)
        else:
            connection_point = Point(to_pos.x - self.constants.STRAIGHT_SEGMENT_LENGTH, to_pos.y)
            extended_end = Point(to_pos.x + self.constants.WIRE_PIN_EXTENSION, to_pos.y)
        return connection_point, extended_end

    def _gentle_arc_path(
        self,
        from_pos: Point,
        rail_x: float,
        y_offset: float,
        connection_point: Point,
        extended_end: Point,
    ) -> list[Point]:
        """Gentle horizontal arc path for wires with similar Y positions."""
        ctrl1 = Point(
            rail_x * self.constants.GENTLE_ARC_CTRL1_RAIL_RATIO
            + from_pos.x * self.constants.GENTLE_ARC_CTRL1_START_RATIO,
            from_pos.y + y_offset * self.constants.GENTLE_ARC_CTRL1_OFFSET_RATIO,
        )
        ctrl2_x = (
            rail_x * self.constants.GENTLE_ARC_CTRL2_RAIL_RATIO
            + connection_point.x * self.constants.GENTLE_ARC_CTRL2_END_RATIO
        )
        ctrl2_y = connection_point.y + y_offset * self.constants.GENTLE_ARC_CTRL2_OFFSET_RATIO
        ctrl2 = Point(ctrl2_x, ctrl2_y)
        return [from_pos, ctrl1, ctrl2, connection_point, extended_end]

    def _s_curve_path(
        self,
        from_pos: Point,
        rail_x: float,
        y_offset: float,
        connection_point: Point,
        extended_end: Point,
    ) -> list[Point]:
        """Smooth S-curve path for wires with vertical separation."""
        ctrl1_x = from_pos.x + (rail_x - from_pos.x) * self.constants.S_CURVE_CTRL1_RATIO
        ctrl1_y = from_pos.y + y_offset * self.constants.S_CURVE_CTRL1_OFFSET_RATIO
        ctrl2_x = (
            connection_point.x + (rail_x - connection_point.x) * self.constants.S_CURVE_CTRL2_RATIO
        )
        ctrl2_y = connection_point.y + y_offset * self.constants.S_CURVE_CTRL2_OFFSET_RATIO

        return [
            from_pos,
            Point(ctrl1_x, ctrl1_y),
            Point(ctrl2_x, ctrl2_y),
            connection_point,
            extended_end,
        ]

    def _right_to_right_path(
        self,
        from_pos: Point,
        connection_point: Point,
        extended_end: Point,
        y_offset: float,
    ) -> list[Point]:
        """Wire path for right-side output to another device."""
        dy = connection_point.y - from_pos.y
        dx = connection_point.x - from_pos.x

        if abs(dy) < self.constants.SIMILAR_Y_THRESHOLD:
            mid_x = from_pos.x + dx * 0.5
            ctrl1 = Point(mid_x, from_pos.y + y_offset * 0.3)
            ctrl2 = Point(mid_x, connection_point.y + y_offset * 0.3)
        else:
            ctrl1_x = from_pos.x + dx * 0.3
            ctrl1_y = from_pos.y + y_offset * 0.5
            ctrl2_x = from_pos.x + dx * 0.7
            ctrl2_y = connection_point.y + y_offset * 0.5
            ctrl1 = Point(ctrl1_x, ctrl1_y)
            ctrl2 = Point(ctrl2_x, ctrl2_y)

        return [from_pos, ctrl1, ctrl2, connection_point, extended_end]


class OverlapResolver:
    """Detects and resolves overlapping wire paths."""

    def __init__(self, config: LayoutConfig, constants: LayoutConstants):
        self.config = config
        self.constants = constants

    def resolve(self, wires: list[dict], path_generator: PathGenerator) -> dict[int, float]:
        """Detect overlapping wire paths and return offset adjustments."""
        start_time = time.perf_counter()
        adjustments: dict[int, float] = {}
        min_separation = self.config.wire_spacing * self.constants.MIN_SEPARATION_MULTIPLIER

        total_wire_pairs = 0
        bbox_rejections = 0
        distance_checks = 0

        wire_samples = []
        for wire in wires:
            path_points = path_generator.calculate_path(
                wire["from_pos"],
                wire["to_pos"],
                wire["rail_x"],
                wire["y_offset"],
                wire["conn"].style,
                wire["is_source_right_side"],
                wire["is_target_right_side"],
            )

            samples = []
            min_x = float("inf")
            max_x = float("-inf")
            min_y = float("inf")
            max_y = float("-inf")

            for i in range(len(path_points) - 1):
                p1, p2 = path_points[i], path_points[i + 1]
                for t in self.constants.SAMPLE_POSITIONS:
                    x = p1.x + (p2.x - p1.x) * t
                    y = p1.y + (p2.y - p1.y) * t
                    samples.append((x, y))
                    min_x = min(min_x, x)
                    max_x = max(max_x, x)
                    min_y = min(min_y, y)
                    max_y = max(max_y, y)

            wire_samples.append(
                {
                    "wire_idx": wire["wire_idx"],
                    "samples": samples,
                    "from_y": wire["from_pos"].y,
                    "bbox": (min_x, max_x, min_y, max_y),
                }
            )

        conflicts = []
        for i in range(len(wire_samples)):
            for j in range(i + 1, len(wire_samples)):
                total_wire_pairs += 1
                wire_a = wire_samples[i]
                wire_b = wire_samples[j]

                if (
                    abs(wire_a["from_y"] - wire_b["from_y"])
                    > self.constants.FROM_Y_POSITION_TOLERANCE
                ):
                    bbox_rejections += 1
                    continue

                bbox_a = wire_a["bbox"]
                bbox_b = wire_b["bbox"]
                bbox_margin = min_separation

                if (
                    bbox_a[1] + bbox_margin < bbox_b[0]
                    or bbox_b[1] + bbox_margin < bbox_a[0]
                    or bbox_a[3] + bbox_margin < bbox_b[2]
                    or bbox_b[3] + bbox_margin < bbox_a[2]
                ):
                    bbox_rejections += 1
                    continue

                distance_checks += 1

                min_dist = float("inf")
                found_conflict = False
                for pa in wire_a["samples"]:
                    for pb in wire_b["samples"]:
                        dist = math.sqrt((pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2)
                        if dist < min_separation:
                            min_dist = dist
                            found_conflict = True
                            break
                        min_dist = min(min_dist, dist)
                    if found_conflict:
                        break

                if min_dist < min_separation:
                    conflicts.append(
                        {
                            "wire_a": wire_a["wire_idx"],
                            "wire_b": wire_b["wire_idx"],
                            "severity": min_separation - min_dist,
                        }
                    )

        for conflict in sorted(conflicts, key=lambda c: c["severity"], reverse=True):
            adjustment_amount = conflict["severity"] / self.constants.CONFLICT_ADJUSTMENT_DIVISOR
            wire_a_idx = conflict["wire_a"]
            wire_b_idx = conflict["wire_b"]

            current_a = adjustments.get(wire_a_idx, 0.0)
            current_b = adjustments.get(wire_b_idx, 0.0)

            adjustments[wire_a_idx] = max(
                -self.constants.MAX_ADJUSTMENT,
                min(self.constants.MAX_ADJUSTMENT, current_a - adjustment_amount),
            )
            adjustments[wire_b_idx] = max(
                -self.constants.MAX_ADJUSTMENT,
                min(self.constants.MAX_ADJUSTMENT, current_b + adjustment_amount),
            )

        elapsed_time = time.perf_counter() - start_time
        if total_wire_pairs > 0:
            rejection_rate = (bbox_rejections / total_wire_pairs) * 100
            logger.debug(
                f"Wire conflict detection: {len(wires)} wires, "
                f"{total_wire_pairs} pairs checked, "
                f"{bbox_rejections} rejected ({rejection_rate:.1f}%), "
                f"{distance_checks} distance checks, "
                f"{len(conflicts)} conflicts found in {elapsed_time * 1000:.1f}ms"
            )

        return adjustments


@dataclass(frozen=True)
class _Box:
    """Axis-aligned bounding box for obstacle avoidance during routing."""

    x1: float
    y1: float
    x2: float
    y2: float
    name: str


def _snap_ceil(val: float, grid: float) -> float:
    return math.ceil((val - 1e-9) / grid) * grid


def _snap_floor(val: float, grid: float) -> float:
    return math.floor((val + 1e-9) / grid) * grid


def _intervals_overlap(a1: float, a2: float, b1: float, b2: float) -> bool:
    lo_a, hi_a = (a1, a2) if a1 <= a2 else (a2, a1)
    lo_b, hi_b = (b1, b2) if b1 <= b2 else (b2, b1)
    return min(hi_a, hi_b) > max(lo_a, lo_b) + 1e-6


def _h_seg_hits_box(x1: float, x2: float, y: float, box: _Box) -> bool:
    lo_x, hi_x = (x1, x2) if x1 <= x2 else (x2, x1)
    return min(hi_x, box.x2) > max(lo_x, box.x1) + 1e-6 and box.y1 - 1e-6 <= y <= box.y2 + 1e-6


def _v_seg_hits_box(x: float, y1: float, y2: float, box: _Box) -> bool:
    lo_y, hi_y = (y1, y2) if y1 <= y2 else (y2, y1)
    return box.x1 - 1e-6 <= x <= box.x2 + 1e-6 and min(hi_y, box.y2) > max(lo_y, box.y1) + 1e-6


def _dedupe_orthogonal_points(points: list[Point]) -> list[Point]:
    """Remove duplicate and collinear intermediate points while keeping the final lead-in."""
    if len(points) <= 2:
        return points

    body = points[:-1]
    ext_end = points[-1]

    deduped: list[Point] = []
    for pt in body:
        if not deduped or abs(pt.x - deduped[-1].x) > 1e-6 or abs(pt.y - deduped[-1].y) > 1e-6:
            deduped.append(pt)

    simplified: list[Point] = []
    for pt in deduped:
        simplified.append(pt)
        while len(simplified) >= 3:
            a, b, c = simplified[-3], simplified[-2], simplified[-1]
            same_x = abs(a.x - b.x) < 1e-6 and abs(b.x - c.x) < 1e-6
            same_y = abs(a.y - b.y) < 1e-6 and abs(b.y - c.y) < 1e-6
            if same_x or same_y:
                simplified.pop(-2)
            else:
                break

    if abs(ext_end.x - simplified[-1].x) > 1e-6 or abs(ext_end.y - simplified[-1].y) > 1e-6:
        simplified.append(ext_end)
    return simplified


class _OrthogonalRouter:
    """Grid-aligned nested-L staircase router with cross-tier corridor avoidance."""

    _TITLE_PAD = 18.0
    _BOARD_LABEL_PAD = 24.0

    def __init__(
        self,
        config: LayoutConfig,
        constants: LayoutConstants,
        board_margin_left: float,
        board_margin_top: float,
        grid_size: float,
    ):
        self.config = config
        self.constants = constants
        self.board_margin_left = board_margin_left
        self.board_margin_top = board_margin_top
        self.grid = max(1.0, grid_size)
        self._used_v: dict[float, list[tuple[float, float]]] = {}
        self._used_h: dict[float, list[tuple[float, float]]] = {}

    def route(self, diagram: Diagram, wires: list[WireData]) -> list[RoutedWire]:
        """Route a list of WireData orthogonally on grid-snapped tracks."""
        board_box = _Box(
            self.board_margin_left,
            self.board_margin_top,
            self.board_margin_left + diagram.board.width,
            self.board_margin_top + diagram.board.height,
            "__board__",
        )
        board_corridor_box = _Box(
            board_box.x1,
            board_box.y1,
            board_box.x2,
            board_box.y2 + (self._BOARD_LABEL_PAD if diagram.show_board_name else 0.0),
            "__board__",
        )

        boxes: dict[str, _Box] = {"__board__": board_box}
        corridor_boxes: list[_Box] = [board_corridor_box]
        for dev in diagram.devices:
            box = _Box(
                dev.position.x,
                dev.position.y,
                dev.position.x + dev.width,
                dev.position.y + dev.height,
                dev.name,
            )
            boxes[dev.name] = box
            corridor_boxes.append(
                _Box(
                    box.x1,
                    max(0.0, box.y1 - self._TITLE_PAD),
                    box.x2,
                    box.y2,
                    dev.name,
                )
            )

        all_boxes = list(boxes.values())
        path_gen = PathGenerator(self.constants)

        # Group wires by (source_entity, source_side) for deterministic staircase fan-out
        side_groups: dict[tuple[str, str], list[tuple[int, WireData]]] = {}
        for idx, wire in enumerate(wires):
            src_name = wire.source_device.name if wire.source_device is not None else "__board__"
            src_is_right = wire.is_source_right_side if wire.source_device is not None else True
            side = "right" if src_is_right else "left"
            side_groups.setdefault((src_name, side), []).append((idx, wire))

        routed_by_idx: dict[int, RoutedWire] = {}

        def _group_sort_key(item: tuple[tuple[str, str], list[tuple[int, WireData]]]):
            (name, side), _ = item
            b = boxes[name]
            return (0 if name == "__board__" else 1, b.x1, b.y1, side)

        for (src_name, side), group in sorted(side_groups.items(), key=_group_sort_key):
            src_box = boxes[src_name]
            src_is_right = side == "right"
            src_dir = 1.0 if src_is_right else -1.0
            base_x = (
                _snap_ceil(src_box.x2 + self.grid, self.grid)
                if src_is_right
                else _snap_floor(src_box.x1 - self.grid, self.grid)
            )

            up = [item for item in group if item[1].to_pos.y < item[1].from_pos.y - 1e-6]
            dn = [item for item in group if item[1].to_pos.y > item[1].from_pos.y + 1e-6]
            flat = [item for item in group if abs(item[1].to_pos.y - item[1].from_pos.y) <= 1e-6]

            # Outermost pin gets step=1 (closest to box), innermost pin gets step=k (furthest)
            up.sort(key=lambda item: (item[1].from_pos.y, item[1].to_pos.y))
            dn.sort(key=lambda item: (-item[1].from_pos.y, -item[1].to_pos.y))
            flat.sort(key=lambda item: abs(item[1].to_pos.x - item[1].from_pos.x))

            ordered_with_steps: list[tuple[int, WireData, int]] = []
            for idx_w, w in flat:
                ordered_with_steps.append((idx_w, w, 1))
            for step_idx, (idx_w, w) in enumerate(up, start=1):
                ordered_with_steps.append((idx_w, w, step_idx))
            for step_idx, (idx_w, w) in enumerate(dn, start=1):
                ordered_with_steps.append((idx_w, w, step_idx))

            for orig_idx, wire, step in ordered_with_steps:
                dst_box = boxes[wire.device.name]
                conn_pt, ext_end = path_gen._connection_points(
                    wire.to_pos, wire.is_target_right_side
                )
                desired_x = base_x + src_dir * (step - 1) * self.grid

                points = self._route_single_wire(
                    diagram=diagram,
                    wire=wire,
                    src_box=src_box,
                    dst_box=dst_box,
                    src_is_right=src_is_right,
                    desired_src_x=desired_x,
                    conn_pt=conn_pt,
                    ext_end=ext_end,
                    all_boxes=all_boxes,
                    corridor_boxes=corridor_boxes,
                )

                routed_by_idx[orig_idx] = RoutedWire(
                    connection=wire.connection,
                    path_points=points,
                    color=wire.color,
                    from_pin_pos=wire.from_pos,
                    to_pin_pos=wire.to_pos,
                    wire_style=WireStyle.ORTHOGONAL,
                )

        return [routed_by_idx[i] for i in range(len(wires))]

    def _v_track_free(self, x: float, y1: float, y2: float, boxes: list[_Box]) -> bool:
        for box in boxes:
            if _v_seg_hits_box(x, y1, y2, box):
                return False
        for seg_y1, seg_y2 in self._used_v.get(round(x, 6), ()):
            if _intervals_overlap(y1, y2, seg_y1, seg_y2):
                return False
        return True

    def _h_track_free(self, y: float, x1: float, x2: float, boxes: list[_Box]) -> bool:
        for box in boxes:
            if _h_seg_hits_box(x1, x2, y, box):
                return False
        for seg_x1, seg_x2 in self._used_h.get(round(y, 6), ()):
            if _intervals_overlap(x1, x2, seg_x1, seg_x2):
                return False
        return True

    def _reserve_v(self, x: float, y1: float, y2: float) -> None:
        if abs(y2 - y1) > 1e-6:
            self._used_v.setdefault(round(x, 6), []).append((min(y1, y2), max(y1, y2)))

    def _reserve_h(self, y: float, x1: float, x2: float) -> None:
        if abs(x2 - x1) > 1e-6:
            self._used_h.setdefault(round(y, 6), []).append((min(x1, x2), max(x1, x2)))

    def _stubs_clear(
        self,
        from_pos: Point,
        x_track: float,
        conn_pt: Point,
        to_y: float,
        src_box: _Box,
        dst_box: _Box,
        corridor_boxes: list[_Box],
    ) -> bool:
        src_edge_x = src_box.x2 if x_track > src_box.x2 else src_box.x1
        dst_edge_x = dst_box.x2 if conn_pt.x > dst_box.x2 else dst_box.x1
        for box in corridor_boxes:
            if box.name != src_box.name and _h_seg_hits_box(src_edge_x, x_track, from_pos.y, box):
                return False
            if box.name != dst_box.name and _h_seg_hits_box(x_track, dst_edge_x, to_y, box):
                return False
        return True

    def _find_gutter_x(
        self,
        start_x: float,
        step_dir: float,
        y1: float,
        y2: float,
        all_boxes: list[_Box],
        max_steps: int = 80,
    ) -> float:
        for i in range(max_steps):
            cand = start_x + i * step_dir * self.grid
            if self._v_track_free(cand, y1, y2, all_boxes):
                return cand
        return start_x

    def _find_corridor_y(
        self,
        diagram: Diagram,
        from_y: float,
        to_y: float,
        min_x: float,
        max_x: float,
        corridor_boxes: list[_Box],
    ) -> float:
        blocking = [b for b in corridor_boxes if b.x1 <= max_x + 1e-6 and b.x2 >= min_x - 1e-6]
        if not blocking:
            for cand in (to_y, from_y):
                snapped = _snap_ceil(cand, self.grid)
                if self._h_track_free(snapped, min_x, max_x, corridor_boxes):
                    return snapped

        title_min_y = (
            _snap_ceil(
                self.config.board_margin_top_base
                + self.config.title_height
                + self.config.title_margin,
                self.grid,
            )
            if diagram.show_title
            else self.grid
        )

        candidates: list[float] = []

        # Top corridor above all blocking boxes
        if blocking:
            top_y = _snap_floor(min(b.y1 for b in blocking) - self.grid, self.grid)
            while top_y >= title_min_y and not self._h_track_free(
                top_y, min_x, max_x, corridor_boxes
            ):
                top_y -= self.grid
            if top_y >= title_min_y:
                candidates.append(top_y)

            # Bottom corridor below all blocking boxes
            bot_y = _snap_ceil(max(b.y2 for b in blocking) + self.grid, self.grid)
            while not self._h_track_free(bot_y, min_x, max_x, corridor_boxes):
                bot_y += self.grid
            candidates.append(bot_y)

            # Mid-tier gap corridors between vertically separated blocking boxes
            intervals = sorted((b.y1, b.y2) for b in blocking)
            merged: list[list[float]] = []
            for y1, y2 in intervals:
                if not merged or y1 > merged[-1][1]:
                    merged.append([y1, y2])
                else:
                    merged[-1][1] = max(merged[-1][1], y2)

            for idx in range(len(merged) - 1):
                gap_top = merged[idx][1]
                gap_bot = merged[idx + 1][0]
                if gap_bot - gap_top >= 2 * self.grid:
                    cy = _snap_ceil(gap_top + self.grid, self.grid)
                    max_gap_y = _snap_floor(gap_bot - self.grid, self.grid)
                    while cy <= max_gap_y:
                        if cy >= title_min_y and self._h_track_free(
                            cy, min_x, max_x, corridor_boxes
                        ):
                            candidates.append(cy)
                            break
                        cy += self.grid
        else:
            bot_y = _snap_ceil(max(from_y, to_y), self.grid)
            while not self._h_track_free(bot_y, min_x, max_x, corridor_boxes):
                bot_y += self.grid
            candidates.append(bot_y)

        return min(candidates, key=lambda cy: abs(from_y - cy) + abs(cy - to_y))

    def _route_single_wire(
        self,
        diagram: Diagram,
        wire: WireData,
        src_box: _Box,
        dst_box: _Box,
        src_is_right: bool,
        desired_src_x: float,
        conn_pt: Point,
        ext_end: Point,
        all_boxes: list[_Box],
        corridor_boxes: list[_Box],
    ) -> list[Point]:
        from_pos = wire.from_pos
        to_pos = wire.to_pos
        src_dir = 1.0 if src_is_right else -1.0
        dst_entry_right = wire.is_target_right_side

        # 1. Straight horizontal wire when pins align and exit/entry face each other cleanly
        if abs(to_pos.y - from_pos.y) <= 1e-6:
            facing_clean = (
                src_is_right and not dst_entry_right and src_box.x2 <= conn_pt.x + 1e-6
            ) or (not src_is_right and dst_entry_right and src_box.x1 >= conn_pt.x - 1e-6)
            if facing_clean:
                src_edge_x = src_box.x2 if src_is_right else src_box.x1
                dst_edge_x = dst_box.x2 if dst_entry_right else dst_box.x1
                other_boxes = [
                    b for b in corridor_boxes if b.name not in (src_box.name, dst_box.name)
                ]
                if self._h_track_free(from_pos.y, src_edge_x, dst_edge_x, other_boxes):
                    self._reserve_h(from_pos.y, src_edge_x, dst_edge_x)
                    return _dedupe_orthogonal_points([from_pos, conn_pt, ext_end])

        # 2. Try 1-track direct Z/L route in the open gutter between source and target
        def _within_target_entry_bound(x_val: float) -> bool:
            if dst_entry_right:
                return x_val >= conn_pt.x + self.grid - 1e-6
            return x_val <= conn_pt.x - self.grid + 1e-6

        def _within_src_exit_bound(x_val: float) -> bool:
            if src_is_right:
                return x_val >= src_box.x2 + self.grid - 1e-6
            return x_val <= src_box.x1 - self.grid + 1e-6

        if _within_src_exit_bound(desired_src_x) and _within_target_entry_bound(desired_src_x):
            for m in range(40):
                cand_x = desired_src_x + m * src_dir * self.grid
                if not _within_target_entry_bound(cand_x):
                    break
                if self._v_track_free(
                    cand_x, from_pos.y, to_pos.y, all_boxes
                ) and self._stubs_clear(
                    from_pos, cand_x, conn_pt, to_pos.y, src_box, dst_box, corridor_boxes
                ):
                    self._reserve_v(cand_x, from_pos.y, to_pos.y)
                    return _dedupe_orthogonal_points(
                        [
                            from_pos,
                            Point(cand_x, from_pos.y),
                            Point(cand_x, to_pos.y),
                            conn_pt,
                            ext_end,
                        ]
                    )

        # 3. Cross-tier / reverse-direction / blocked gutter: route via top/bottom/gap corridor
        src_gutter_x = self._find_gutter_x(
            desired_src_x, src_dir, from_pos.y, from_pos.y, all_boxes
        )
        dst_step_dir = 1.0 if dst_entry_right else -1.0
        dst_base_x = (
            _snap_ceil(max(dst_box.x2 + self.grid, conn_pt.x + self.grid), self.grid)
            if dst_entry_right
            else _snap_floor(min(dst_box.x1 - self.grid, conn_pt.x - self.grid), self.grid)
        )
        dst_gutter_x = self._find_gutter_x(dst_base_x, dst_step_dir, to_pos.y, to_pos.y, all_boxes)

        min_x = min(src_gutter_x, dst_gutter_x)
        max_x = max(src_gutter_x, dst_gutter_x)
        corridor_y = self._find_corridor_y(
            diagram, from_pos.y, to_pos.y, min_x, max_x, corridor_boxes
        )

        # Ensure vertical gutter segments along [from_y..corridor_y] and [corridor_y..to_y]
        # remain clear of boxes and existing vertical tracks.
        src_gutter_x = self._find_gutter_x(src_gutter_x, src_dir, from_pos.y, corridor_y, all_boxes)
        dst_gutter_x = self._find_gutter_x(
            dst_gutter_x, dst_step_dir, corridor_y, to_pos.y, all_boxes
        )

        self._reserve_v(src_gutter_x, from_pos.y, corridor_y)
        self._reserve_h(
            corridor_y, min(src_gutter_x, dst_gutter_x), max(src_gutter_x, dst_gutter_x)
        )
        self._reserve_v(dst_gutter_x, corridor_y, to_pos.y)

        return _dedupe_orthogonal_points(
            [
                from_pos,
                Point(src_gutter_x, from_pos.y),
                Point(src_gutter_x, corridor_y),
                Point(dst_gutter_x, corridor_y),
                Point(dst_gutter_x, to_pos.y),
                conn_pt,
                ext_end,
            ]
        )


class WireRouter:
    """
    Routes wires between board pins and device pins.

    Thin orchestrator that delegates to internal components:
    - WireDataCollector: resolves pin positions from diagram
    - RailAssigner: assigns routing rails to prevent crossings
    - OverlapResolver: detects and resolves wire conflicts
    - PathGenerator: generates Bezier curve paths
    - _OrthogonalRouter: routes grid-aligned staircase orthogonal wires
    """

    def __init__(self, config: LayoutConfig, board_margin_left: float, board_margin_top: float):
        self.config = config
        self.constants = LayoutConstants()
        self.board_margin_left = board_margin_left
        self.board_margin_top = board_margin_top

    def route_wires(self, diagram: Diagram) -> list[RoutedWire]:
        """Route all wires using the diagram's wire style (or per-connection overrides)."""
        collector = WireDataCollector(self.board_margin_left, self.board_margin_top, self.constants)
        wire_data = collector.collect(diagram)

        grid_size = float(diagram.grid_size if diagram.grid_size > 0 else self.config.grid_size)
        if diagram.wire_style == WireStyle.ORTHOGONAL:
            ortho_router = _OrthogonalRouter(
                self.config,
                self.constants,
                self.board_margin_left,
                self.board_margin_top,
                grid_size,
            )
            return ortho_router.route(diagram, wire_data)

        curved_data = [w for w in wire_data if w.connection.style != WireStyle.ORTHOGONAL]
        ortho_data = [w for w in wire_data if w.connection.style == WireStyle.ORTHOGONAL]

        routed_curved: list[RoutedWire] = []
        if curved_data:
            curved_data.sort(key=lambda w: (w.from_pos.y, w.device.position.y, w.to_pos.y))
            y_groups = collector.group_by_position(curved_data)

            assigner = RailAssigner(self.config, self.constants, self.board_margin_left)
            initial_wires = assigner.assign(curved_data, diagram.board.width, y_groups)

            path_gen = PathGenerator(self.constants)
            resolver = OverlapResolver(self.config, self.constants)
            adjustments = resolver.resolve(initial_wires, path_gen)

            routed_curved = path_gen.generate(initial_wires, adjustments)

        routed_ortho: list[RoutedWire] = []
        if ortho_data:
            ortho_router = _OrthogonalRouter(
                self.config,
                self.constants,
                self.board_margin_left,
                self.board_margin_top,
                grid_size,
            )
            routed_ortho = ortho_router.route(diagram, ortho_data)

        return routed_curved + routed_ortho
