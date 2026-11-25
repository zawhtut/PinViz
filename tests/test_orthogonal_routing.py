"""Unit tests for grid-aligned staircase orthogonal routing, tier spacing, and viewport sizing."""

import xml.etree.ElementTree as ET

import pytest
from pydantic import ValidationError

from pinviz import boards
from pinviz.config_loader import ConfigLoader
from pinviz.layout import (
    CanvasSizer,
    LayoutConfig,
    LayoutEngine,
    WireRouter,
    create_orthogonal_path,
)
from pinviz.model import Connection, Device, DevicePin, Diagram, PinRole, Point, WireStyle
from pinviz.render_svg import SVGRenderer
from pinviz.schemas import DiagramConfigSchema


def _segments_from_points(points: list[Point]) -> list[tuple[Point, Point]]:
    return [(points[i], points[i + 1]) for i in range(len(points) - 1)]


def _segment_intersects_interior(
    p1: Point, p2: Point, box: tuple[float, float, float, float]
) -> bool:
    """Return True if axis-aligned segment (p1, p2) passes through box interior."""
    x1, y1, x2, y2 = box
    if abs(p1.y - p2.y) < 1e-6:
        y = p1.y
        if not (y1 + 1e-6 < y < y2 - 1e-6):
            return False
        lo, hi = min(p1.x, p2.x), max(p1.x, p2.x)
        return min(hi, x2) > max(lo, x1) + 1e-6
    if abs(p1.x - p2.x) < 1e-6:
        x = p1.x
        if not (x1 + 1e-6 < x < x2 - 1e-6):
            return False
        lo, hi = min(p1.y, p2.y), max(p1.y, p2.y)
        return min(hi, y2) > max(lo, y1) + 1e-6
    return True


def _perpendicular_cross(a1: Point, a2: Point, b1: Point, b2: Point) -> bool:
    """Return True if orthogonal segments a and b cross each other in their strict interiors."""
    a_horiz = abs(a1.y - a2.y) < 1e-6
    b_horiz = abs(b1.y - b2.y) < 1e-6
    if a_horiz == b_horiz:
        return False
    h1, h2 = (a1, a2) if a_horiz else (b1, b2)
    v1, v2 = (b1, b2) if a_horiz else (a1, a2)
    hx_lo, hx_hi = min(h1.x, h2.x), max(h1.x, h2.x)
    vy_lo, vy_hi = min(v1.y, v2.y), max(v1.y, v2.y)
    return (hx_lo + 1e-6 < v1.x < hx_hi - 1e-6) and (vy_lo + 1e-6 < h1.y < vy_hi - 1e-6)


class TestSchemaAndModelWireStyle:
    """Tests for wire_style and grid_size configuration plumbing."""

    def test_schema_defaults(self):
        schema = DiagramConfigSchema(
            title="Test",
            board="raspberry_pi_5",
            devices=[{"type": "bh1750"}],
            connections=[],
        )
        assert schema.wire_style == "curved"
        assert schema.grid_size == 10

    def test_schema_rejects_invalid_wire_style_and_grid_size(self):
        with pytest.raises(ValidationError):
            DiagramConfigSchema(title="Test", wire_style="diagonal")

        with pytest.raises(ValidationError):
            DiagramConfigSchema(title="Test", grid_size=0)

    def test_config_loader_propagates_wire_style_and_grid_size(self):
        diagram = ConfigLoader(emit_validation_output=False).load_from_dict(
            {
                "title": "Ortho Test",
                "board": "raspberry_pi_5",
                "wire_style": "orthogonal",
                "grid_size": 15,
                "devices": [{"type": "bh1750", "name": "BH1750"}],
                "connections": [{"board_pin": 1, "device": "BH1750", "device_pin": "VCC"}],
            }
        )
        assert diagram.wire_style == WireStyle.ORTHOGONAL
        assert diagram.grid_size == 15


class TestOrthogonalPathBuilder:
    """Tests for create_orthogonal_path SVG path generation."""

    def test_emits_axis_aligned_commands_with_rounded_corners(self):
        pts = [Point(220, 140), Point(260, 140), Point(260, 180), Point(440, 180)]
        path_d = create_orthogonal_path(pts, corner_radius=5.0)
        assert path_d.startswith("M 220.00,140.00")
        assert " H " in path_d
        assert " V " in path_d
        assert " Q 260.00,140.00 " in path_d
        assert " Q 260.00,180.00 " in path_d
        assert " C " not in path_d

    def test_zero_corner_radius_omits_curves(self):
        pts = [Point(0, 0), Point(50, 0), Point(50, 50)]
        path_d = create_orthogonal_path(pts, corner_radius=0.0)
        assert path_d == "M 0.00,0.00 H 50.00 V 50.00"


class TestStaircaseOrthogonalRouter:
    """Tests for grid alignment, nested-L staircase fan-out, and cross-tier routing."""

    def test_staircase_upward_and_downward_no_self_intersection(self):
        board = boards.raspberry_pi_5()
        # Source device in Tier 0 with 6 right-side pins: top 3 go UP, bottom 3 go DOWN
        src = Device(
            name="Hub",
            width=80,
            height=100,
            pins=[
                DevicePin("IN", PinRole.POWER_3V3, Point(5, 50)),
                DevicePin("U1", PinRole.GPIO, Point(75, 15)),
                DevicePin("U2", PinRole.GPIO, Point(75, 25)),
                DevicePin("U3", PinRole.GPIO, Point(75, 35)),
                DevicePin("D1", PinRole.GPIO, Point(75, 65)),
                DevicePin("D2", PinRole.GPIO, Point(75, 75)),
                DevicePin("D3", PinRole.GPIO, Point(75, 85)),
            ],
        )
        top_target = Device(
            name="TopTarget",
            width=80,
            height=60,
            pins=[
                DevicePin("P1", PinRole.GPIO, Point(5, 10)),
                DevicePin("P2", PinRole.GPIO, Point(5, 20)),
                DevicePin("P3", PinRole.GPIO, Point(5, 30)),
            ],
        )
        bot_target = Device(
            name="BotTarget",
            width=80,
            height=60,
            pins=[
                DevicePin("P1", PinRole.GPIO, Point(5, 10)),
                DevicePin("P2", PinRole.GPIO, Point(5, 20)),
                DevicePin("P3", PinRole.GPIO, Point(5, 30)),
            ],
        )
        conns = [
            # Anchor Hub low (pin 39) so TopTarget sits above Hub and BotTarget sits below
            Connection(39, "Hub", "IN"),
            Connection(1, "TopTarget", "P1"),
            # Upward-bound from Hub right side
            Connection.from_device("Hub", "U1", "TopTarget", "P1"),
            Connection.from_device("Hub", "U2", "TopTarget", "P2"),
            Connection.from_device("Hub", "U3", "TopTarget", "P3"),
            # Downward-bound from Hub right side
            Connection.from_device("Hub", "D1", "BotTarget", "P1"),
            Connection.from_device("Hub", "D2", "BotTarget", "P2"),
            Connection.from_device("Hub", "D3", "BotTarget", "P3"),
        ]
        diagram = Diagram(
            title="Staircase Fan-Out",
            board=board,
            devices=[src, top_target, bot_target],
            connections=conns,
            wire_style=WireStyle.ORTHOGONAL,
            grid_size=10,
        )
        result = LayoutEngine().layout_diagram(diagram)

        # Verify every orthogonal wire is strictly axis-aligned and grid-snapped
        hub_wires = [w for w in result.routed_wires if w.connection.source_device == "Hub"]
        assert len(hub_wires) == 6
        for wire in hub_wires:
            assert wire.wire_style == WireStyle.ORTHOGONAL
            for p1, p2 in _segments_from_points(wire.path_points):
                assert abs(p1.x - p2.x) < 1e-6 or abs(p1.y - p2.y) < 1e-6
            # Intermediate routing vertices (excluding pin endpoints and lead-in) align to grid
            for pt in wire.path_points[1:-2]:
                assert pt.x % 10 == 0

        # Upward group: innermost (U3) extends furthest right; outermost (U1) is closest
        up_x = {
            w.connection.source_pin: w.path_points[1].x
            for w in hub_wires
            if w.connection.source_pin.startswith("U")
        }
        assert up_x["U1"] < up_x["U2"] < up_x["U3"]

        # Downward group: innermost (D1) extends furthest right; outermost (D3) is closest
        dn_x = {
            w.connection.source_pin: w.path_points[1].x
            for w in hub_wires
            if w.connection.source_pin.startswith("D")
        }
        assert dn_x["D3"] < dn_x["D2"] < dn_x["D1"]

        # No self-intersection within upward or downward staircase groups
        for group_prefix in ("U", "D"):
            segs = [
                seg
                for w in hub_wires
                if w.connection.source_pin.startswith(group_prefix)
                for seg in _segments_from_points(w.path_points)
            ]
            for i in range(len(segs)):
                for j in range(i + 1, len(segs)):
                    assert not _perpendicular_cross(*segs[i], *segs[j])

    def test_cross_tier_wires_avoid_all_device_boxes(self):
        board = boards.raspberry_pi_5()
        tier0 = Device(
            name="Tier0",
            width=80,
            height=80,
            pins=[
                DevicePin("VIN", PinRole.POWER_5V, Point(5, 20)),
                DevicePin("OUT", PinRole.GPIO, Point(75, 40)),
            ],
        )
        tier1 = Device(
            name="Tier1",
            width=80,
            height=80,
            pins=[
                DevicePin("IN", PinRole.GPIO, Point(5, 40)),
                DevicePin("OUT", PinRole.GPIO, Point(75, 60)),
            ],
        )
        tier2 = Device(
            name="Tier2",
            width=80,
            height=80,
            pins=[
                DevicePin("IN", PinRole.GPIO, Point(5, 40)),
                DevicePin("SKIP_IN", PinRole.POWER_3V3, Point(5, 60)),
            ],
        )
        conns = [
            Connection(2, "Tier0", "VIN"),
            Connection.from_device("Tier0", "OUT", "Tier1", "IN"),
            Connection.from_device("Tier1", "OUT", "Tier2", "IN"),
            # Cross-tier wire skipping Tier0 and Tier1 straight from board to Tier2
            Connection(1, "Tier2", "SKIP_IN"),
        ]
        diagram = Diagram(
            title="Cross-Tier Routing",
            board=board,
            devices=[tier0, tier1, tier2],
            connections=conns,
            wire_style=WireStyle.ORTHOGONAL,
            grid_size=15,
        )
        result = LayoutEngine().layout_diagram(diagram)

        boxes = [
            (d.position.x, d.position.y, d.position.x + d.width, d.position.y + d.height)
            for d in diagram.devices
        ]

        for wire in result.routed_wires:
            interior_segs = _segments_from_points(wire.path_points)[1:-1]
            for p1, p2 in interior_segs:
                for box in boxes:
                    assert not _segment_intersects_interior(p1, p2, box), (
                        f"Wire {wire.connection} segment {p1}->{p2} crossed box {box}"
                    )

        skip_wire = next(
            w for w in result.routed_wires if w.connection.device_pin_name == "SKIP_IN"
        )
        assert len(skip_wire.path_points) >= 6
        for pt in skip_wire.path_points[1:-2]:
            assert pt.x % 15 == 0
        corridor_ys = [
            p1.y
            for p1, p2 in _segments_from_points(skip_wire.path_points)[1:-1]
            if abs(p1.y - p2.y) < 1e-6
        ]
        assert any(y % 15 == 0 for y in corridor_ys)

    def test_reverse_direction_right_to_left_corridor_routing(self):
        board = boards.raspberry_pi_5()
        left_dev = Device(
            name="LeftMod",
            width=80,
            height=80,
            position=Point(450, 150),
            pins=[DevicePin("LEFT_IN", PinRole.GPIO, Point(5, 40))],
        )
        mid_dev = Device(
            name="MidMod",
            width=80,
            height=80,
            position=Point(630, 150),
            pins=[DevicePin("PASS", PinRole.GPIO, Point(5, 40))],
        )
        right_dev = Device(
            name="RightMod",
            width=80,
            height=80,
            position=Point(810, 150),
            pins=[DevicePin("RIGHT_OUT", PinRole.GPIO, Point(75, 40))],
        )
        diagram = Diagram(
            title="Reverse Corridor Routing",
            board=board,
            devices=[left_dev, mid_dev, right_dev],
            connections=[Connection.from_device("RightMod", "RIGHT_OUT", "LeftMod", "LEFT_IN")],
            wire_style=WireStyle.ORTHOGONAL,
            grid_size=10,
        )
        config = LayoutConfig()
        board_margin_top = config.get_board_margin_top(diagram.show_title)
        routed = WireRouter(config, config.board_margin_left, board_margin_top).route_wires(diagram)
        _cw, ch = CanvasSizer(config, board_margin_top).calculate_canvas_size(diagram, routed)

        assert len(routed) == 1
        wire = routed[0]
        assert len(wire.path_points) >= 6
        # Exits right side of RightMod (x > 890) before looping to LeftMod left side (x < 450)
        assert wire.path_points[1].x >= right_dev.position.x + right_dev.width + 10
        assert wire.path_points[-3].x <= left_dev.position.x - 10
        for pt in wire.path_points[1:-2]:
            assert pt.x % 10 == 0
        for p1, p2 in _segments_from_points(wire.path_points):
            assert abs(p1.x - p2.x) < 1e-6 or abs(p1.y - p2.y) < 1e-6
            assert p1.y < ch and p2.y < ch

        boxes = [
            (d.position.x, d.position.y, d.position.x + d.width, d.position.y + d.height)
            for d in diagram.devices
        ]
        for p1, p2 in _segments_from_points(wire.path_points)[1:-1]:
            for box in boxes:
                assert not _segment_intersects_interior(p1, p2, box)


class TestVerticalSpacingAndCanvasBounds:
    """Tests for external title clearance and unclipped bottom viewport sizing."""

    def test_vertical_spacing_never_collapses_below_title_clearance(self):
        board = boards.raspberry_pi_5()
        devs = [
            Device(
                name=f"Module {i}",
                width=80,
                height=50,
                pins=[DevicePin("IN", PinRole.GPIO, Point(5, 10))],
            )
            for i in range(6)
        ]
        conns = [Connection(i + 1, f"Module {i}", "IN") for i in range(6)]
        diagram = Diagram(
            title="Dense Tier",
            board=board,
            devices=devs,
            connections=conns,
            wire_style=WireStyle.ORTHOGONAL,
        )

        # Even with a tiny requested spacing, floor protects external top title labels
        config = LayoutConfig(device_spacing_vertical=4.0)
        result = LayoutEngine(config).layout_diagram(diagram)

        ordered = sorted(diagram.devices, key=lambda d: d.position.y)
        for upper, lower in zip(ordered, ordered[1:], strict=False):
            gap = lower.position.y - (upper.position.y + upper.height)
            assert gap >= 20.0

        # Canvas must contain all devices and routed wires without clipping
        for d in diagram.devices:
            assert d.position.y + d.height < result.canvas_height
        for wire in result.routed_wires:
            for pt in wire.path_points:
                assert pt.y < result.canvas_height

    def test_tall_multi_tier_diagram_not_clamped_at_3000px(self, tmp_path):
        board = boards.raspberry_pi_5()
        # 40 stacked modules exceeds 3000px height
        devs = [
            Device(
                name=f"TallMod_{i}",
                width=80,
                height=65,
                pins=[DevicePin("IN", PinRole.GPIO, Point(5, 10))],
            )
            for i in range(40)
        ]
        conns = [Connection((i % 40) + 1, f"TallMod_{i}", "IN") for i in range(40)]
        diagram = Diagram(
            title="Very Tall Diagram",
            board=board,
            devices=devs,
            connections=conns,
            wire_style=WireStyle.ORTHOGONAL,
        )

        result = LayoutEngine().layout_diagram(diagram)
        max_dev_bottom = max(d.position.y + d.height for d in devs)
        assert max_dev_bottom > 3000.0
        assert result.canvas_height >= max_dev_bottom + 40.0

        out_svg = tmp_path / "tall_ortho.svg"
        SVGRenderer().render(diagram, out_svg)
        root = ET.parse(out_svg).getroot()
        vb_parts = [float(v) for v in root.attrib["viewBox"].split()]
        assert vb_parts[3] == pytest.approx(result.canvas_height)
