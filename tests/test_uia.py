"""Tests for dibs.uia (find/near/render helpers) and the ui_tree/find/click_element actions.

No real Windows UIA involved: `uia.get_tree` is monkeypatched with a fake tree built from plain
dicts, matching the pattern desk.* uses in test_actions.py.
"""

from __future__ import annotations

import pytest

from dibs import actions, desk, uia

TWO_SCREENS = [
    desk.Screen(index=0, x=0, y=0, width=2560, height=1440, primary=True),
    desk.Screen(index=1, x=2560, y=0, width=2560, height=1440, primary=False),
]

WINDOW = desk.Window(
    hwnd=42,
    title="Calculator",
    process="Calculator.exe",
    rect=(100, 100, 500, 700),
    visible=True,
    foreground=True,
)


def _node(
    id_, role, name, rect, *, value=None, depth=1, enabled=True, offscreen=False
) -> uia.UiaNode:
    return uia.UiaNode(
        id=id_,
        role=role,
        name=name,
        value=value,
        rect=rect,
        depth=depth,
        enabled=enabled,
        offscreen=offscreen,
    )


# Fake Calculator-ish tree, absolute screen px, all inside WINDOW.rect.
FAKE_TREE = [
    _node(1, "Window", "Calculator", (100, 100, 500, 700), depth=0),
    _node(2, "Text", "Display is 0", (110, 110, 480, 160), value="0", depth=1),
    _node(3, "Button", "Seven", (110, 400, 200, 450), depth=1),
    _node(4, "Button", "Eight", (210, 400, 300, 450), depth=1),
    _node(5, "Button", "Nine", (310, 400, 400, 450), depth=1),
    _node(6, "Button", "Equals", (110, 600, 200, 650), depth=1),
    _node(7, "Button", "Memory recall", (410, 200, 480, 250), enabled=False, depth=1),
]


@pytest.fixture(autouse=True)
def fake_screens(monkeypatch):
    monkeypatch.setattr(desk, "list_screens", lambda: TWO_SCREENS)
    yield


@pytest.fixture
def fake_window(monkeypatch):
    monkeypatch.setattr(desk, "list_windows", lambda: [WINDOW])
    yield


@pytest.fixture
def fake_tree(monkeypatch):
    monkeypatch.setattr(uia, "get_tree", lambda window, **kw: list(FAKE_TREE))
    yield


# ---------------------------------------------------------------------------
# uia.py unit tests
# ---------------------------------------------------------------------------


def test_find_nodes_case_insensitive_substring():
    matches = uia.find_nodes(FAKE_TREE, "seven")
    assert [m.id for m in matches] == [3]


def test_find_nodes_matches_value_too():
    matches = uia.find_nodes(FAKE_TREE, "0")
    assert 2 in [m.id for m in matches]


def test_find_nodes_role_filter():
    matches = uia.find_nodes(FAKE_TREE, "e", role="Button")
    assert all(m.role == "Button" for m in matches)
    assert 2 not in [m.id for m in matches]  # Text node excluded by role filter


def test_find_nodes_exact_requires_full_match():
    assert uia.find_nodes(FAKE_TREE, "Seven", exact=True)
    assert not uia.find_nodes(FAKE_TREE, "Seve", exact=True)


def test_rank_by_near_orders_by_distance_to_nearest_near_match():
    matches = uia.find_nodes(FAKE_TREE, "e", role="Button")  # Seven, Eight, Nine, Equals
    near = uia.find_nodes(FAKE_TREE, "Display")
    ranked = uia.rank_by_near(matches, near)
    # "Memory recall" (410,200-480,250) sits closest to the display among the matches.
    assert ranked[0].name == "Memory recall"


def test_rank_by_near_noop_with_no_near_matches():
    matches = uia.find_nodes(FAKE_TREE, "Seven")
    assert uia.rank_by_near(matches, []) == matches


def test_render_text_indents_by_depth_and_marks_disabled():
    text = uia.render_text(FAKE_TREE)
    lines = text.splitlines()
    assert lines[0].startswith("[1] Window")
    seven_line = next(line for line in lines if "Seven" in line)
    assert seven_line.startswith("  ")  # depth 1 -> one indent
    recall_line = next(line for line in lines if "Memory recall" in line)
    assert "disabled" in recall_line


def test_resolve_window_by_hwnd(fake_window):
    win = uia.resolve_window(hwnd=42)
    assert win.title == "Calculator"


def test_resolve_window_by_title_substring(fake_window):
    win = uia.resolve_window(title="calc")
    assert win.hwnd == 42


def test_resolve_window_falls_back_to_foreground(fake_window):
    win = uia.resolve_window()
    assert win.hwnd == 42


def test_resolve_window_not_found(fake_window):
    with pytest.raises(uia.UiaError):
        uia.resolve_window(title="nope")
    with pytest.raises(uia.UiaError):
        uia.resolve_window(hwnd=999)


# ---------------------------------------------------------------------------
# actions.run_action: ui_tree / find / click_element
# ---------------------------------------------------------------------------


def test_ui_tree_action_returns_nodes_with_screenshot_space_rects(fake_window, fake_tree):
    result = actions.run_action({"action": "ui_tree", "hwnd": 42})
    assert result.data["hwnd"] == 42
    nodes = result.data["nodes"]
    assert len(nodes) == len(FAKE_TREE)
    seven = next(n for n in nodes if n["name"] == "Seven")
    assert seven["rect"] == [110, 400, 200, 450]
    scale = actions.scale_for(TWO_SCREENS[0], 1568, 1_150_000)
    assert seven["rect_shot"] == [
        round(110 * scale),
        round(400 * scale),
        round(200 * scale),
        round(450 * scale),
    ]
    assert "Seven" in result.text


def test_ui_tree_action_roles_filter(fake_window, monkeypatch):
    def fake_get_tree(window, *, max_depth, max_nodes, roles=None):
        if roles is not None:
            return [n for n in FAKE_TREE if n.role in roles]
        return list(FAKE_TREE)

    monkeypatch.setattr(uia, "get_tree", fake_get_tree)
    result = actions.run_action({"action": "ui_tree", "hwnd": 42, "roles": ["Button"]})
    assert all(n["role"] == "Button" for n in result.data["nodes"])


def test_find_action_returns_best_match_with_center(fake_window, fake_tree):
    result = actions.run_action({"action": "find", "text": "Seven", "hwnd": 42})
    match = result.data["match"]
    assert match["name"] == "Seven"
    assert match["center"] == [155, 425]
    scale = actions.scale_for(TWO_SCREENS[0], 1568, 1_150_000)
    assert match["center_shot"] == [round(155 * scale), round(425 * scale)]


def test_find_action_near_reorders_matches(fake_window, fake_tree):
    result = actions.run_action(
        {
            "action": "find",
            "text": "e",
            "role": "Button",
            "near": "Display",
            "hwnd": 42,
        }
    )
    assert result.data["match"]["name"] == "Memory recall"


def test_find_action_not_found_lists_nearby_names(fake_window, fake_tree):
    with pytest.raises(actions.ActionError) as ei:
        actions.run_action({"action": "find", "text": "does-not-exist", "hwnd": 42})
    assert ei.value.code == "not_found"
    assert "Seven" in ei.value.detail


def test_find_action_alternates_capped_at_five(fake_window, monkeypatch):
    many = [_node(i, "Button", "Digit", (i, i, i + 10, i + 10), depth=1) for i in range(1, 9)]
    monkeypatch.setattr(uia, "get_tree", lambda window, **kw: many)
    result = actions.run_action({"action": "find", "text": "Digit", "hwnd": 42})
    assert len(result.data["alternates"]) == 5


def test_click_element_clicks_center_and_returns_node(fake_window, fake_tree, monkeypatch):
    calls = []
    monkeypatch.setattr(desk, "click", lambda x, y, **kw: calls.append((x, y, kw)))
    result = actions.run_action({"action": "click_element", "text": "Seven", "hwnd": 42})
    assert calls == [(155, 425, {"button": "left", "clicks": 1})]
    assert result.data["absolute"] == [155, 425]
    assert result.data["node"]["name"] == "Seven"


def test_click_element_double_and_button(fake_window, fake_tree, monkeypatch):
    calls = []
    monkeypatch.setattr(desk, "click", lambda x, y, **kw: calls.append(kw))
    actions.run_action(
        {"action": "click_element", "text": "Seven", "hwnd": 42, "button": "right", "double": True}
    )
    assert calls == [{"button": "right", "clicks": 2}]


def test_click_element_not_found(fake_window, fake_tree):
    with pytest.raises(actions.ActionError) as ei:
        actions.run_action({"action": "click_element", "text": "nope", "hwnd": 42})
    assert ei.value.code == "not_found"


def test_ui_tree_and_find_are_read_only_click_element_is_not():
    assert actions.is_read_only("ui_tree")
    assert actions.is_read_only("find")
    assert not actions.is_read_only("click_element")


def test_ui_tree_window_not_found(fake_window):
    with pytest.raises(actions.ActionError) as ei:
        actions.run_action({"action": "ui_tree", "hwnd": 999})
    assert ei.value.code == "window_not_found"
