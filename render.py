from __future__ import annotations
import json
import pathlib
import sys

BASE_PATH = pathlib.Path(__file__).parent
RAW_PATH = BASE_PATH / "raw.txt"
CONTEXT_PATH = BASE_PATH / "context.txt"
BOOK_PATH = BASE_PATH / "book.txt"

CLICKABLE_ROLES = frozenset({
    "Button", "MenuItem", "ListItem", "Hyperlink", "TabItem", "TreeItem",
    "SplitButton", "CheckBox", "RadioButton", "Slider", "ScrollBar",
    "Spinner", "DataItem", "Document",
})
WRITABLE_ROLES = frozenset({"Edit", "ComboBox"})


def parse_raw(lines: list[str]) -> tuple[dict, list[dict], dict, list[dict], list[dict], dict, list[dict]]:
    """Parse raw.txt sections in order: screen, hwnds, focused, probes, windows, z_order, tree_nodes."""
    pos = 0
    screen = json.loads(lines[pos])
    pos += 1
    hwnds: list[dict] = []
    while pos < len(lines):
        obj = json.loads(lines[pos])
        if "hwnd" in obj:
            hwnds.append(obj)
            pos += 1
        else:
            break
    focused = json.loads(lines[pos])
    pos += 1
    probes: list[dict] = []
    while pos < len(lines):
        obj = json.loads(lines[pos])
        if "probe_px" in obj:
            probes.append(obj)
            pos += 1
        else:
            break
    windows: list[dict] = []
    while pos < len(lines):
        obj = json.loads(lines[pos])
        if "wnd_role" in obj:
            windows.append(obj)
            pos += 1
        else:
            break
    z_order = json.loads(lines[pos])
    pos += 1
    tree_nodes: list[dict] = []
    while pos < len(lines):
        obj = json.loads(lines[pos])
        if "t_depth" in obj:
            tree_nodes.append(obj)
            pos += 1
        else:
            break
    return screen, hwnds, focused, probes, windows, z_order, tree_nodes


def classify_element(role: str, enabled: bool, readonly: bool) -> str:
    if not enabled:
        return "none"
    if role in WRITABLE_ROLES and not readonly:
        return "type"
    if role in CLICKABLE_ROLES:
        return "click"
    return "none"


def merge_probe_into_tree(tree_nodes: list[dict], probes: list[dict]) -> list[dict]:
    tree_keys: set[tuple] = set()
    for node in tree_nodes:
        key = (node["t_role"], node["t_name"], node["t_x"], node["t_y"], node["t_w"], node["t_h"])
        tree_keys.add(key)
    merged = list(tree_nodes)
    for probe in probes:
        if not probe.get("p_role"):
            continue
        key = (probe["p_role"], probe.get("p_name", ""), probe["p_x"], probe["p_y"], probe["p_w"], probe["p_h"])
        if key not in tree_keys:
            # Inherit hwnd from the target window (probes are on focused window)
            merged.append({
                "t_wnd": "", "t_hwnd": 0,
                "t_depth": 0,
                "t_role": probe["p_role"],
                "t_name": probe.get("p_name", ""),
                "t_aid": probe.get("p_aid", ""),
                "t_desc": probe.get("p_desc", ""),
                "t_x": probe["p_x"],
                "t_y": probe["p_y"],
                "t_w": probe["p_w"],
                "t_h": probe["p_h"],
                "t_enabled": probe.get("p_enabled", True),
                "t_focus": probe.get("p_focus", False),
                "t_value": probe.get("p_value", ""),
                "t_readonly": probe.get("p_readonly", False),
                "t_offscreen": probe.get("p_offscreen", False),
            })
            tree_keys.add(key)
    return merged


def build_context(screen: dict, focused: dict, windows: list[dict],
                  tree_nodes: list[dict], probes: list[dict]) -> tuple[str, list[dict]]:
    sw, sh = screen["sw"], screen["sh"]
    book_entries: list[dict] = []
    output_lines: list[str] = []
    # Find target window(s) and their hwnds
    target_windows: list[dict] = []
    other_windows: list[str] = []
    focused_hwnd = focused.get("focused_hwnd", 0)
    for win in windows:
        if win["wnd_target"]:
            target_windows.append(win)
            output_lines.append(f"[{win['wnd_name']}]")
        else:
            other_windows.append(f"  [{win['wnd_name']}]")
    # Determine hwnd for probe-sourced elements (they belong to focused window)
    probe_hwnd = focused_hwnd
    probe_wnd_name = focused.get("focused_title", "")
    SKIP_NAMELESS = frozenset({"Pane", "Group", "Custom", "Image", "Separator", "Thumb",
                               "ProgressBar", "Header", "HeaderItem"})
    merged_nodes = merge_probe_into_tree(tree_nodes, probes)
    # Fill in probe elements' hwnd/wnd from focused
    for node in merged_nodes:
        if not node.get("t_hwnd"):
            node["t_hwnd"] = probe_hwnd
        if not node.get("t_wnd"):
            node["t_wnd"] = probe_wnd_name
    seq = 0
    for node in merged_nodes:
        role = node["t_role"]
        x, y, w, h = node["t_x"], node["t_y"], node["t_w"], node["t_h"]
        if w <= 0 or h <= 0:
            continue
        name = node["t_name"]
        value = node["t_value"]
        if role in SKIP_NAMELESS and not name and not value:
            continue
        enabled = node.get("t_enabled", True)
        readonly = node.get("t_readonly", False)
        offscreen = node.get("t_offscreen", False)
        if offscreen:
            continue
        action_tag = classify_element(role, enabled, readonly)
        if action_tag == "none" and not name and not value:
            continue
        seq += 1
        node_id = str(seq)
        book_entries.append({
            "id": node_id, "role": role, "name": name,
            "hwnd": node["t_hwnd"], "wnd": node["t_wnd"],
            "px": x, "py": y, "pw": w, "ph": h,
            "enabled": enabled, "readonly": readonly, "action": action_tag,
        })
        tag_str = f"[{action_tag.upper()}]" if action_tag != "none" else ""
        line = f"{'  ' * (node['t_depth'] + 1)}{node_id}. {tag_str} {role}"
        line += f" '{name}'" * bool(name)
        line += f" val='{value}'" * bool(value)
        line += " disabled" * (not enabled)
        line += " *" * node["t_focus"]
        output_lines.append(line)
    output_lines.extend(other_windows)
    return "\n".join(output_lines), book_entries


def pipeline() -> None:
    raw_lines = RAW_PATH.read_text(encoding="utf-8").splitlines()
    screen, hwnds, focused, probes, windows, z_order, tree_nodes = parse_raw(raw_lines)
    context_text, book_entries = build_context(screen, focused, windows, tree_nodes, probes)
    CONTEXT_PATH.write_text(context_text, encoding="utf-8")
    BOOK_PATH.write_text("\n".join(json.dumps(e) for e in book_entries), encoding="utf-8")
    sys.stderr.write(f"render: {len(tree_nodes)} tree + {len(probes)} probe -> {len(book_entries)} selectors\n")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    pipeline()
    sys.stdout.write(CONTEXT_PATH.read_text(encoding="utf-8"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
