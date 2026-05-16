from __future__ import annotations
import json

CLICKABLE_ROLES = frozenset({
    "Button", "MenuItem", "ListItem", "Hyperlink", "TabItem", "TreeItem",
    "SplitButton", "CheckBox", "RadioButton", "Slider", "ScrollBar",
    "Spinner", "DataItem", "Document",
})
WRITABLE_ROLES = frozenset({"Edit", "ComboBox"})
SKIP_NAMELESS = frozenset({
    "Pane", "Group", "Custom", "Image", "Separator", "Thumb",
    "ProgressBar", "Header", "HeaderItem",
})


def parse_raw(lines: list[str]) -> tuple[dict, list[dict], dict, list[dict], list[dict], dict, list[dict]]:
    pos = 0
    screen = json.loads(lines[pos]); pos += 1
    hwnds: list[dict] = []
    while pos < len(lines) and "hwnd" in (obj := json.loads(lines[pos])):
        hwnds.append(obj); pos += 1
    focused = json.loads(lines[pos]); pos += 1
    probes: list[dict] = []
    while pos < len(lines) and "probe_px" in (obj := json.loads(lines[pos])):
        probes.append(obj); pos += 1
    windows: list[dict] = []
    while pos < len(lines) and "wnd_role" in (obj := json.loads(lines[pos])):
        windows.append(obj); pos += 1
    z_order = json.loads(lines[pos]); pos += 1
    tree_nodes: list[dict] = []
    while pos < len(lines) and "t_depth" in (obj := json.loads(lines[pos])):
        tree_nodes.append(obj); pos += 1
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
    tree_keys = {
        (n["t_role"], n["t_name"], n["t_x"], n["t_y"], n["t_w"], n["t_h"])
        for n in tree_nodes
    }
    merged = list(tree_nodes)
    for p in probes:
        if not p.get("p_role"):
            continue
        key = (p["p_role"], p.get("p_name", ""), p["p_x"], p["p_y"], p["p_w"], p["p_h"])
        if key not in tree_keys:
            merged.append({
                "t_wnd": "", "t_hwnd": 0, "t_depth": 0,
                "t_role": p["p_role"], "t_name": p.get("p_name", ""),
                "t_aid": p.get("p_aid", ""), "t_desc": p.get("p_desc", ""),
                "t_x": p["p_x"], "t_y": p["p_y"], "t_w": p["p_w"], "t_h": p["p_h"],
                "t_enabled": p.get("p_enabled", True), "t_focus": p.get("p_focus", False),
                "t_value": p.get("p_value", ""), "t_readonly": p.get("p_readonly", False),
                "t_offscreen": p.get("p_offscreen", False),
            })
            tree_keys.add(key)
    return merged


def build_context(screen: dict, focused: dict, windows: list[dict],
                  z_order: dict, tree_nodes: list[dict], probes: list[dict]) -> tuple[str, list[dict]]:
    book_entries: list[dict] = []
    output_lines: list[str] = []
    other_windows: list[str] = []
    focused_hwnd = focused.get("focused_hwnd", 0)
    probe_wnd_name = focused.get("focused_title", "")

    for win in windows:
        if win["wnd_target"]:
            output_lines.append(f"[{win['wnd_name']}]")
        else:
            other_windows.append(f"  [{win['wnd_name']}]")

    merged_nodes = merge_probe_into_tree(tree_nodes, probes)
    for node in merged_nodes:
        if not node.get("t_hwnd"):
            node["t_hwnd"] = focused_hwnd
        if not node.get("t_wnd"):
            node["t_wnd"] = probe_wnd_name

    seq = 0
    for node in merged_nodes:
        role, x, y, w, h = node["t_role"], node["t_x"], node["t_y"], node["t_w"], node["t_h"]
        if w <= 0 or h <= 0:
            continue
        name, value = node["t_name"], node["t_value"]
        if role in SKIP_NAMELESS and not name and not value:
            continue
        enabled = node.get("t_enabled", True)
        readonly = node.get("t_readonly", False)
        if node.get("t_offscreen", False):
            continue
        action_tag = classify_element(role, enabled, readonly)
        if action_tag == "none" and not name and not value:
            continue
        seq += 1
        node_id = str(seq)
        book_entries.append({
            "id": node_id, "role": role, "name": name, "value": value,
            "hwnd": node["t_hwnd"], "wnd": node["t_wnd"],
            "px": x, "py": y, "pw": w, "ph": h,
            "enabled": enabled, "readonly": readonly, "action": action_tag,
        })
        tag_str = f"[{action_tag.upper()}]" if action_tag != "none" else ""
        line = f"{'  ' * (node['t_depth'] + 1)}{node_id}. {tag_str} {role}"
        line += f" '{name}'" * bool(name)
        if value:
            vis = value[:80] + "…" if len(value) > 80 else value
            line += f" val='{vis}'"
        line += " disabled" * (not enabled)
        line += " *" * node["t_focus"]
        output_lines.append(line)

    output_lines.extend(other_windows)

    # Append z-order section so LLM knows window stacking
    z_list = z_order.get("z_order", [])
    if z_list:
        output_lines.append("")
        output_lines.append("Z-ORDER (front to back):")
        for entry in z_list[:8]:
            marker = " [FOCUSED]" if entry["z"] == 0 else ""
            output_lines.append(f"  z={entry['z']} hwnd={entry['hwnd']} \"{entry['title']}\"{marker}")

    return "\n".join(output_lines), book_entries


def pipeline(raw_lines: list[str]) -> tuple[str, list[dict]]:
    screen, hwnds, focused, probes, windows, z_order, tree_nodes = parse_raw(raw_lines)
    return build_context(screen, focused, windows, z_order, tree_nodes, probes)
