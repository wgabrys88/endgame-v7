from __future__ import annotations
import argparse
import ctypes
import ctypes.wintypes as W
import json
import logging
import pathlib
import sys
import time
import uuid
from collections import deque

log = logging.getLogger("collector")
logging.basicConfig(stream=sys.stderr, format="%(levelname)s %(message)s")

BASE_PATH = pathlib.Path(__file__).parent
OUTPUT_PATH = BASE_PATH / "raw.txt"
INTERACTION_LOG_PATH = BASE_PATH / "interaction_log.jsonl"
VERIFIED_STATE_PATH = BASE_PATH / "verified_state.txt"

ole32 = ctypes.OleDLL("ole32")
oleaut32 = ctypes.WinDLL("oleaut32")
oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
oleaut32.SysFreeString.restype = None
oleaut32.SafeArrayDestroy.argtypes = [ctypes.c_void_p]
oleaut32.SafeArrayDestroy.restype = ctypes.HRESULT
user32 = ctypes.WinDLL("user32", use_last_error=True)

ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD), ("Data4", W.BYTE * 8)]


class VARIANT(ctypes.Structure):
    _fields_ = [("vt", W.WORD), ("r1", W.WORD), ("r2", W.WORD), ("r3", W.WORD), ("val", ctypes.c_ulonglong)]


class SAFEARRAY_BOUND(ctypes.Structure):
    _fields_ = [("cElements", W.DWORD), ("lLbound", W.LONG)]


class SAFEARRAY(ctypes.Structure):
    _fields_ = [("cDims", W.USHORT), ("fFeatures", W.USHORT), ("cbElements", W.DWORD),
                ("cLocks", W.DWORD), ("pvData", ctypes.c_void_p), ("rgsabound", SAFEARRAY_BOUND * 1)]


def make_guid(s: str) -> GUID:
    b = uuid.UUID(s).bytes
    return GUID(int.from_bytes(b[0:4], "big"), int.from_bytes(b[4:6], "big"),
                int.from_bytes(b[6:8], "big"), (ctypes.c_ubyte * 8)(*b[8:16]))


CLSID_CUIAutomation = make_guid("ff48dba4-60ef-4201-aa87-54103eef594e")
IID_IUIAutomation = make_guid("30cbe57d-d9d0-452a-ab13-7ac5ac4825ee")
IID_LegacyIAccessible = make_guid("828055ad-355b-4435-86d5-3b51c14a9b1b")

CONTROL_TYPE_MAP: dict[int, str] = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar", 50015: "Slider",
    50016: "Spinner", 50017: "StatusBar", 50018: "Tab", 50019: "TabItem",
    50020: "Text", 50021: "ToolBar", 50022: "ToolTip", 50023: "Tree",
    50024: "TreeItem", 50025: "Custom", 50026: "Group", 50027: "Thumb",
    50028: "DataGrid", 50029: "DataItem", 50030: "Document", 50031: "SplitButton",
    50032: "Window", 50033: "Pane", 50034: "Header", 50035: "HeaderItem",
    50036: "Table", 50037: "TitleBar", 50038: "Separator",
}

_uia: ctypes.c_void_p = ctypes.c_void_p()
_true_cond: ctypes.c_void_p = ctypes.c_void_p()


def vt(this, idx, proto_args, *args):
    vtable = ctypes.cast(this, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, *proto_args)
    return proto(vtable[idx])(this, *args)


def release(ptr) -> None:
    raw = ptr if isinstance(ptr, int) else (ptr.value if hasattr(ptr, "value") else ptr)
    vt_ptr = ctypes.c_void_p(raw)
    vtable = ctypes.cast(vt_ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    proto(vtable[2])(vt_ptr)


def get_str(el, prop_id: int) -> str:
    var = VARIANT()
    vt(el, 10, (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(VARIANT)),
       ctypes.c_int(prop_id), ctypes.byref(var))
    if var.vt == 8:
        ptr = ctypes.c_void_p(var.val)
        s = ctypes.cast(ptr, ctypes.c_wchar_p).value or ""
        oleaut32.SysFreeString(ptr)
        return s
    return ""


def get_int(el, prop_id: int) -> int:
    var = VARIANT()
    vt(el, 10, (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(VARIANT)),
       ctypes.c_int(prop_id), ctypes.byref(var))
    if var.vt == 3:
        return int(var.val & 0xFFFFFFFF)
    return 0


def get_bool(el, prop_id: int) -> bool:
    var = VARIANT()
    vt(el, 10, (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(VARIANT)),
       ctypes.c_int(prop_id), ctypes.byref(var))
    if var.vt == 11:
        return (var.val & 0xFFFF) == 0xFFFF
    return False


def get_rect(el) -> tuple[int, int, int, int]:
    var = VARIANT()
    vt(el, 10, (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(VARIANT)),
       ctypes.c_int(30001), ctypes.byref(var))
    if var.vt == 8197:
        sa_ptr = ctypes.c_void_p(var.val)
        sa = ctypes.cast(sa_ptr, ctypes.POINTER(SAFEARRAY)).contents
        d = (ctypes.c_double * 4).from_address(sa.pvData)
        result = (int(d[0]), int(d[1]), int(d[2]), int(d[3]))
        oleaut32.SafeArrayDestroy(sa_ptr)
        return result
    return (0, 0, 0, 0)


def get_legacy_value(el) -> str:
    pattern = ctypes.c_void_p()
    hr = vt(el, 14,
            (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)),
            ctypes.c_int(10018), ctypes.byref(IID_LegacyIAccessible), ctypes.byref(pattern))
    if hr != 0 or not pattern.value:
        return ""
    bstr = ctypes.c_wchar_p()
    hr2 = vt(pattern, 8, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)), ctypes.byref(bstr))
    result = bstr.value or "" if hr2 == 0 else ""
    release(pattern)
    return result


def get_legacy_readonly(el) -> bool:
    pattern = ctypes.c_void_p()
    hr = vt(el, 14,
            (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)),
            ctypes.c_int(10018), ctypes.byref(IID_LegacyIAccessible), ctypes.byref(pattern))
    if hr != 0 or not pattern.value:
        return False
    var = VARIANT()
    hr2 = vt(pattern, 11,
             (ctypes.c_void_p, ctypes.POINTER(VARIANT)),
             ctypes.byref(var))
    result = (var.val & 0xFFFF) == 0xFFFF if hr2 == 0 and var.vt == 11 else False
    release(pattern)
    return result


def element_from_point(px: int, py: int) -> ctypes.c_void_p | None:
    pt = ctypes.c_long * 2
    found = ctypes.c_void_p()
    hr = vt(_uia, 7,
            (ctypes.c_void_p, ctypes.c_long * 2, ctypes.POINTER(ctypes.c_void_p)),
            pt(px, py), ctypes.byref(found))
    if hr == 0 and found.value:
        return found
    return None


def get_children_raw(el) -> list[ctypes.c_void_p]:
    arr = ctypes.c_void_p()
    hr = vt(el, 6,
            (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
            ctypes.c_int(0x2), _true_cond, ctypes.byref(arr))
    if hr != 0 or not arr.value:
        return []
    length = ctypes.c_int()
    vt(arr, 3, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)), ctypes.byref(length))
    children: list[ctypes.c_void_p] = []
    for i in range(length.value):
        child = ctypes.c_void_p()
        vt(arr, 4, (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)),
           ctypes.c_int(i), ctypes.byref(child))
        if child.value:
            children.append(child)
    release(arr)
    return children


# ─── PHASES ───────────────────────────────────────────────────────────────────


def phase_dpi() -> None:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    log.info("dpi set")


def phase_com_init() -> None:
    global _uia, _true_cond
    ole32.CoInitialize(None)
    ole32.CoCreateInstance(
        ctypes.byref(CLSID_CUIAutomation), None, 1,
        ctypes.byref(IID_IUIAutomation), ctypes.byref(_uia))
    vt(_uia, 21, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)), ctypes.byref(_true_cond))
    log.info("com ready")


def phase_screen(out: list[str]) -> tuple[int, int]:
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    out.append(json.dumps({"sw": sw, "sh": sh}))
    log.info(f"screen {sw}x{sh}")
    return sw, sh


def phase_hwnds(out: list[str]) -> None:
    title_buf = ctypes.create_unicode_buffer(512)
    class_buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [W.HWND]
    user32.IsWindowVisible.restype = W.BOOL
    user32.GetWindow.argtypes = [W.HWND, W.UINT]
    user32.GetWindow.restype = W.HWND
    count = 0

    def walk_children(parent_hwnd: int, depth: int) -> None:
        nonlocal count
        child = user32.GetWindow(W.HWND(parent_hwnd), 5)
        while child:
            user32.GetWindowTextW(child, title_buf, 512)
            user32.GetClassNameW(child, class_buf, 256)
            visible = user32.IsWindowVisible(child)
            out.append(json.dumps({
                "hwnd": int(child), "parent": parent_hwnd, "depth": depth,
                "class": class_buf.value, "title": title_buf.value, "visible": bool(visible),
            }))
            count += 1
            walk_children(int(child), depth + 1)
            child = user32.GetWindow(child, 2)

    def enum_callback(hwnd, lparam):
        nonlocal count
        user32.GetWindowTextW(hwnd, title_buf, 512)
        user32.GetClassNameW(hwnd, class_buf, 256)
        visible = user32.IsWindowVisible(hwnd)
        out.append(json.dumps({
            "hwnd": int(hwnd), "parent": 0, "depth": 0,
            "class": class_buf.value, "title": title_buf.value, "visible": bool(visible),
        }))
        count += 1
        walk_children(int(hwnd), 1)
        return True

    user32.EnumWindows(ENUM_WINDOWS_PROC(enum_callback), 0)
    log.info(f"hwnds: {count}")


def phase_focused(out: list[str]) -> tuple[str, int]:
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    title = buf.value
    out.append(json.dumps({"focused_hwnd": int(hwnd), "focused_title": title}))
    log.info(f"focused: {title}")
    return title, int(hwnd)


def phase_probe(out: list[str], step: int, dwell_ms: int, hwnd: int) -> None:
    rect = W.RECT()
    user32.GetWindowRect(W.HWND(hwnd), ctypes.byref(rect))
    sleep_s = dwell_ms / 1000.0
    count = 0
    for y in range(rect.top, rect.bottom, step):
        for x in range(rect.left, rect.right, step):
            user32.SetCursorPos(x, y)
            time.sleep(sleep_s)
            el = element_from_point(x, y)
            if el and el.value:
                try:
                    ct = get_int(el, 30003)
                    role = CONTROL_TYPE_MAP.get(ct, "")
                    name = get_str(el, 30005)
                    auto_id = get_str(el, 30011)
                    desc = get_str(el, 30159)
                    enabled = get_bool(el, 30010)
                    has_focus = get_bool(el, 30004)
                    offscreen = get_bool(el, 30022)
                    value = get_legacy_value(el)
                    readonly = get_legacy_readonly(el)
                    rx, ry, rw, rh = get_rect(el)
                except OSError:
                    out.append(json.dumps({"probe_px": x, "probe_py": y}))
                    count += 1
                    continue
                out.append(json.dumps({
                    "probe_px": x, "probe_py": y,
                    "p_role": role, "p_name": name, "p_aid": auto_id, "p_desc": desc,
                    "p_x": rx, "p_y": ry, "p_w": rw, "p_h": rh,
                    "p_enabled": enabled, "p_focus": has_focus, "p_offscreen": offscreen,
                    "p_value": value, "p_readonly": readonly,
                }))
            else:
                out.append(json.dumps({"probe_px": x, "probe_py": y}))
            count += 1
    log.info(f"probe: {count} points")


def phase_windows(out: list[str], fg_title: str) -> list[tuple[ctypes.c_void_p, str, int]]:
    """Returns list of (uia_element, wnd_name, hwnd) for target windows."""
    root = ctypes.c_void_p()
    vt(_uia, 5, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)), ctypes.byref(root))
    top_children = get_children_raw(root)
    results: list[tuple[ctypes.c_void_p, str, int]] = []
    for top_el in top_children:
        try:
            x, y, w, h = get_rect(top_el)
            ct = get_int(top_el, 30003)
            role = CONTROL_TYPE_MAP.get(ct, "")
            if role != "Window" or w <= 0 or h <= 0:
                continue
            name = get_str(top_el, 30005)
            # Use property 30020 = NativeWindowHandle
            var = VARIANT()
            vt(top_el, 10, (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(VARIANT)),
               ctypes.c_int(30020), ctypes.byref(var))
            el_hwnd = int(var.val & 0xFFFFFFFF) if var.vt == 3 else 0
        except OSError:
            continue
        is_target = (name == fg_title)
        out.append(json.dumps({
            "wnd_role": role, "wnd_name": name, "wnd_hwnd": el_hwnd,
            "wnd_x": x, "wnd_y": y, "wnd_w": w, "wnd_h": h, "wnd_target": is_target,
        }))
        if is_target:
            results.append((top_el, name, el_hwnd))
    log.info(f"windows: {len(top_children)} top-level, {len(results)} target")
    return results


def phase_z_order(out: list[str]) -> None:
    user32.GetWindow.argtypes = [W.HWND, W.UINT]
    user32.GetWindow.restype = W.HWND
    user32.IsWindowVisible.argtypes = [W.HWND]
    user32.IsWindowVisible.restype = W.BOOL
    user32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    hwnd = user32.GetForegroundWindow()
    z_list: list[dict] = []
    z = 0
    buf = ctypes.create_unicode_buffer(512)
    seen: set[str] = set()
    while hwnd:
        if user32.IsWindowVisible(hwnd):
            user32.GetWindowTextW(hwnd, buf, 512)
            title = buf.value
            if title and title not in seen:
                z_list.append({"z": z, "hwnd": int(hwnd), "title": title})
                seen.add(title)
                z += 1
        hwnd = user32.GetWindow(hwnd, 2)
    out.append(json.dumps({"z_order": z_list}))
    log.info(f"z_order: {len(z_list)} windows")


def phase_tree(out: list[str], target_el, wnd_name: str, wnd_hwnd: int, timeout: float) -> None:
    start = time.perf_counter()
    queue: deque[tuple[ctypes.c_void_p, int]] = deque()
    for child in get_children_raw(target_el):
        queue.append((child, 1))
    count = 0
    actionable_set = {"Button", "Edit", "ComboBox", "ListItem", "Hyperlink", "MenuItem",
                      "TabItem", "SplitButton", "CheckBox", "RadioButton", "Slider",
                      "Document", "Text", "ScrollBar", "TreeItem", "DataItem", "Custom"}
    while queue:
        if time.perf_counter() - start > timeout:
            log.warning("tree timeout")
            break
        raw_el, depth = queue.popleft()
        try:
            x, y, w, h = get_rect(raw_el)
            ct = get_int(raw_el, 30003)
        except OSError:
            continue
        role = CONTROL_TYPE_MAP.get(ct, "")
        if not role:
            try:
                for child in get_children_raw(raw_el):
                    queue.append((child, depth))
            except OSError:
                pass
            continue
        try:
            name = get_str(raw_el, 30005)
            auto_id = get_str(raw_el, 30011)
            desc = get_str(raw_el, 30159)
            enabled = get_bool(raw_el, 30010)
            has_focus = get_bool(raw_el, 30004)
            value = get_legacy_value(raw_el) if role in actionable_set else ""
            readonly = get_legacy_readonly(raw_el) if role in actionable_set else False
            offscreen = get_bool(raw_el, 30022)
        except OSError:
            continue
        out.append(json.dumps({
            "t_wnd": wnd_name, "t_hwnd": wnd_hwnd,
            "t_depth": depth, "t_role": role, "t_name": name, "t_aid": auto_id, "t_desc": desc,
            "t_x": x, "t_y": y, "t_w": w, "t_h": h,
            "t_enabled": enabled, "t_focus": has_focus, "t_value": value,
            "t_readonly": readonly, "t_offscreen": offscreen,
        }))
        count += 1
        try:
            for child in get_children_raw(raw_el):
                queue.append((child, depth + 1))
        except OSError:
            pass
    elapsed = time.perf_counter() - start
    log.info(f"tree [{wnd_name}]: {count} nodes in {elapsed:.3f}s")


def phase_rescan_interacted() -> None:
    """Re-scan all previously interacted elements and write verified_state.txt."""
    if not INTERACTION_LOG_PATH.exists():
        VERIFIED_STATE_PATH.write_text("", encoding="utf-8")
        return
    entries = [json.loads(l) for l in INTERACTION_LOG_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not entries:
        VERIFIED_STATE_PATH.write_text("", encoding="utf-8")
        return
    # Deduplicate by (px, py) — keep latest entry per position
    seen: dict[tuple[int, int], dict] = {}
    for e in entries:
        key = (e["px"], e["py"])
        seen[key] = e
    results: list[str] = []
    buf = ctypes.create_unicode_buffer(512)
    for key, entry in seen.items():
        px, py = key
        el = element_from_point(px, py)
        if not el or not el.value:
            results.append(json.dumps({
                "verified": True, "status": "NOT_FOUND",
                "original_role": entry.get("role", ""), "original_name": entry.get("name", ""),
                "px": px, "py": py, "hwnd": entry.get("hwnd", 0),
            }))
            continue
        try:
            ct = get_int(el, 30003)
            role = CONTROL_TYPE_MAP.get(ct, "")
            name = get_str(el, 30005)
            value = get_legacy_value(el)
            enabled = get_bool(el, 30010)
            rx, ry, rw, rh = get_rect(el)
        except OSError:
            results.append(json.dumps({
                "verified": True, "status": "NOT_FOUND",
                "original_role": entry.get("role", ""), "original_name": entry.get("name", ""),
                "px": px, "py": py, "hwnd": entry.get("hwnd", 0),
            }))
            continue
        # Check if element changed (different role or name)
        orig_role = entry.get("role", "")
        orig_name = entry.get("name", "")
        if role != orig_role or (orig_name and name != orig_name):
            status = "ELEMENT_CHANGED"
        else:
            status = "OK"
        # Also get current window title for this hwnd
        hwnd = entry.get("hwnd", 0)
        wnd_title = ""
        if hwnd:
            user32.GetWindowTextW(W.HWND(hwnd), buf, 512)
            wnd_title = buf.value
        results.append(json.dumps({
            "verified": True, "status": status,
            "original_role": orig_role, "original_name": orig_name,
            "current_role": role, "current_name": name, "current_value": value,
            "current_enabled": enabled,
            "current_x": rx, "current_y": ry, "current_w": rw, "current_h": rh,
            "px": px, "py": py, "hwnd": hwnd, "wnd_title": wnd_title,
        }))
    VERIFIED_STATE_PATH.write_text("\n".join(results), encoding="utf-8")
    log.info(f"rescan: {len(results)} elements verified")


def pipeline(timeout: float, probe_step: int, probe_dwell: int, skip_probe: bool,
             expand_hwnds: list[int] | None) -> None:
    out: list[str] = []
    phase_dpi()
    phase_com_init()
    sw, sh = phase_screen(out)
    # 1. phase_hwnds — full deterministic inventory
    phase_hwnds(out)
    # 2. phase_focused — identify foreground
    fg_title, fg_hwnd = phase_focused(out)
    # 3. phase_probe — hover scan focused window
    if not skip_probe:
        phase_probe(out, probe_step, probe_dwell, fg_hwnd)
    # 4. phase_rescan_interacted — verify previously touched elements
    phase_rescan_interacted()
    # 5. phase_windows — UIA top-level list
    targets = phase_windows(out, fg_title)
    # 6. phase_z_order
    phase_z_order(out)
    # 7. phase_tree — walk target windows (focused + planner-expanded)
    walked_hwnds: set[int] = set()
    for target_el, wnd_name, wnd_hwnd in targets:
        phase_tree(out, target_el, wnd_name, wnd_hwnd, timeout)
        walked_hwnds.add(wnd_hwnd)
    # Walk additional planner-requested windows
    if expand_hwnds:
        root = ctypes.c_void_p()
        vt(_uia, 5, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)), ctypes.byref(root))
        top_children = get_children_raw(root)
        for top_el in top_children:
            try:
                var = VARIANT()
                vt(top_el, 10, (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(VARIANT)),
                   ctypes.c_int(30020), ctypes.byref(var))
                el_hwnd = int(var.val & 0xFFFFFFFF) if var.vt == 3 else 0
            except OSError:
                continue
            if el_hwnd in expand_hwnds and el_hwnd not in walked_hwnds:
                try:
                    name = get_str(top_el, 30005)
                except OSError:
                    name = ""
                phase_tree(out, top_el, name, el_hwnd, timeout)
                walked_hwnds.add(el_hwnd)
    OUTPUT_PATH.write_text("\n".join(out), encoding="utf-8")
    log.info(f"raw.txt written: {len(out)} lines")


def main() -> None:
    parser = argparse.ArgumentParser(prog="collector")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--probe-step", type=int, default=30)
    parser.add_argument("--probe-dwell", type=int, default=10)
    parser.add_argument("--no-probe", action="store_true")
    parser.add_argument("--expand-hwnds", type=str, default="",
                        help="Comma-separated hwnd integers to additionally tree-walk")
    args = parser.parse_args()
    log.setLevel(logging.DEBUG if args.debug else logging.INFO)
    expand = [int(h) for h in args.expand_hwnds.split(",") if h.strip()] if args.expand_hwnds else None
    pipeline(args.timeout, args.probe_step, args.probe_dwell, args.no_probe, expand)


if __name__ == "__main__":
    main()
