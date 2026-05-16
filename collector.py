from __future__ import annotations
import ctypes
import ctypes.wintypes as W
import json
import pathlib
import time
import uuid
from collections import deque

import probe_log

BASE_PATH = pathlib.Path(__file__).parent
INTERACTION_LOG_PATH = BASE_PATH / "interaction_log.jsonl"
VERIFIED_STATE_PATH = BASE_PATH / "verified_state.txt"

DELAY_PROBE_DWELL = 0.01

ole32 = ctypes.OleDLL("ole32")
oleaut32 = ctypes.WinDLL("oleaut32")
oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
oleaut32.SysFreeString.restype = None
oleaut32.SafeArrayDestroy.argtypes = [ctypes.c_void_p]
oleaut32.SafeArrayDestroy.restype = ctypes.HRESULT
user32 = ctypes.WinDLL("user32", use_last_error=True)

user32.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.IsWindowVisible.argtypes = [W.HWND]
user32.IsWindowVisible.restype = W.BOOL
user32.GetWindow.argtypes = [W.HWND, W.UINT]
user32.GetWindow.restype = W.HWND
user32.GetWindowRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]
user32.GetWindowRect.restype = W.BOOL

ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)

UIA_BOUNDING_RECTANGLE = 30001
UIA_CONTROL_TYPE = 30003
UIA_HAS_KEYBOARD_FOCUS = 30004
UIA_NAME = 30005
UIA_IS_ENABLED = 30010
UIA_AUTOMATION_ID = 30011
UIA_NATIVE_WINDOW_HANDLE = 30020
UIA_IS_OFFSCREEN = 30022
UIA_DESCRIPTION = 30159
UIA_LEGACY_IACCESSIBLE_PATTERN = 10018


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
    try:
        return proto(vtable[idx])(this, *args)
    except (OSError, PermissionError) as e:
        # Return a fake failure HRESULT so upper layers can handle it gracefully
        log(f"[COM] vtable call failed with {e}")
        return 0x80070005  # E_ACCESSDENIED


def release(ptr) -> None:
    raw = ptr if isinstance(ptr, int) else (ptr.value if hasattr(ptr, "value") else ptr)
    vt_ptr = ctypes.c_void_p(raw)
    vtable = ctypes.cast(vt_ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    proto(vtable[2])(vt_ptr)


def _get_property(el, prop_id: int) -> VARIANT:
    var = VARIANT()
    vt(el, 10, (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(VARIANT)),
       ctypes.c_int(prop_id), ctypes.byref(var))
    return var


def get_str(el, prop_id: int) -> str:
    var = _get_property(el, prop_id)
    if var.vt == 8:
        ptr = ctypes.c_void_p(var.val)
        s = ctypes.cast(ptr, ctypes.c_wchar_p).value or ""
        oleaut32.SysFreeString(ptr)
        return s
    return ""


def get_int(el, prop_id: int) -> int:
    var = _get_property(el, prop_id)
    return int(var.val & 0xFFFFFFFF) if var.vt == 3 else 0


def get_bool(el, prop_id: int) -> bool:
    var = _get_property(el, prop_id)
    return (var.val & 0xFFFF) == 0xFFFF if var.vt == 11 else False


def get_rect(el) -> tuple[int, int, int, int]:
    var = _get_property(el, UIA_BOUNDING_RECTANGLE)
    if var.vt == 8197:
        sa_ptr = ctypes.c_void_p(var.val)
        sa = ctypes.cast(sa_ptr, ctypes.POINTER(SAFEARRAY)).contents
        d = (ctypes.c_double * 4).from_address(sa.pvData)
        result = (int(d[0]), int(d[1]), int(d[2]), int(d[3]))
        oleaut32.SafeArrayDestroy(sa_ptr)
        return result
    return (0, 0, 0, 0)


def _get_legacy_pattern(el):
    pattern = ctypes.c_void_p()
    hr = vt(el, 14,
            (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)),
            ctypes.c_int(UIA_LEGACY_IACCESSIBLE_PATTERN), ctypes.byref(IID_LegacyIAccessible), ctypes.byref(pattern))
    return pattern if hr == 0 and pattern.value else None


def get_legacy_value(el) -> str:
    pattern = _get_legacy_pattern(el)
    if not pattern:
        return ""
    bstr = ctypes.c_wchar_p()
    hr = vt(pattern, 8, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)), ctypes.byref(bstr))
    result = bstr.value or "" if hr == 0 else ""
    release(pattern)
    return result


def get_legacy_readonly(el) -> bool:
    pattern = _get_legacy_pattern(el)
    if not pattern:
        return False
    var = VARIANT()
    hr = vt(pattern, 11, (ctypes.c_void_p, ctypes.POINTER(VARIANT)), ctypes.byref(var))
    result = (var.val & 0xFFFF) == 0xFFFF if hr == 0 and var.vt == 11 else False
    release(pattern)
    return result


def element_from_point(px: int, py: int) -> ctypes.c_void_p | None:
    try:
        point_packed = ctypes.c_int64(px | (py << 32))
        found = ctypes.c_void_p()
        hr = vt(_uia, 7,
                (ctypes.c_void_p, ctypes.c_int64, ctypes.POINTER(ctypes.c_void_p)),
                point_packed, ctypes.byref(found))
        return found if hr == 0 and found.value else None
    except Exception:
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


def _get_hwnd_from_element(el) -> int:
    var = _get_property(el, UIA_NATIVE_WINDOW_HANDLE)
    return int(var.val & 0xFFFFFFFF) if var.vt == 3 else 0


# --- Tree walker and RuntimeId for hierarchy reconstruction ---

_tree_walker: ctypes.c_void_p = ctypes.c_void_p()


def _ensure_tree_walker() -> None:
    """Create a control-view tree walker (lazy init)."""
    global _tree_walker
    if _tree_walker.value:
        return
    # IUIAutomation::get_ControlViewWalker is vtable index 14
    vt(_uia, 14, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
       ctypes.byref(_tree_walker))


def get_runtime_id(el) -> tuple[int, ...] | None:
    """Get RuntimeId as a tuple of ints for hashing/comparison."""
    # IUIAutomationElement::GetRuntimeId is vtable index 4
    sa_ptr = ctypes.c_void_p()
    hr = vt(el, 4, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
            ctypes.byref(sa_ptr))
    if hr != 0 or not sa_ptr.value:
        return None
    sa = ctypes.cast(sa_ptr, ctypes.POINTER(SAFEARRAY)).contents
    count = sa.rgsabound[0].cElements
    if count == 0:
        oleaut32.SafeArrayDestroy(sa_ptr)
        return None
    data = (ctypes.c_int * count).from_address(sa.pvData)
    result = tuple(data[i] for i in range(count))
    oleaut32.SafeArrayDestroy(sa_ptr)
    return result


def get_parent_element(el) -> ctypes.c_void_p | None:
    """Walk up one level using the control-view tree walker."""
    _ensure_tree_walker()
    parent = ctypes.c_void_p()
    # IUIAutomationTreeWalker::GetParentElement is vtable index 3
    hr = vt(_tree_walker, 3,
            (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
            el, ctypes.byref(parent))
    return parent if hr == 0 and parent.value else None


def snapshot_at(px: int, py: int) -> dict | None:
    el = element_from_point(px, py)
    if not el or not el.value:
        return None
    ct = get_int(el, UIA_CONTROL_TYPE)
    role = CONTROL_TYPE_MAP.get(ct, "")
    name = get_str(el, UIA_NAME)
    value = get_legacy_value(el)
    x, y, w, h = get_rect(el)
    return {"role": role, "name": name, "value": value, "x": x, "y": y, "w": w, "h": h}


def phase_dpi() -> None:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))


def phase_com_init() -> None:
    global _uia, _true_cond
    if _uia.value:
        return
    ole32.CoInitialize(None)
    ole32.CoCreateInstance(
        ctypes.byref(CLSID_CUIAutomation), None, 1,
        ctypes.byref(IID_IUIAutomation), ctypes.byref(_uia))
    vt(_uia, 21, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)), ctypes.byref(_true_cond))


def phase_screen(out: list[str]) -> tuple[int, int]:
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    out.append(json.dumps({"sw": sw, "sh": sh}, ensure_ascii=False))
    return sw, sh


def phase_hwnds(out: list[str]) -> None:
    title_buf = ctypes.create_unicode_buffer(512)
    class_buf = ctypes.create_unicode_buffer(256)

    def walk_children(parent_hwnd: int, depth: int) -> None:
        child = user32.GetWindow(W.HWND(parent_hwnd), 5)
        while child:
            user32.GetWindowTextW(child, title_buf, 512)
            user32.GetClassNameW(child, class_buf, 256)
            out.append(json.dumps({
                "hwnd": int(child), "parent": parent_hwnd, "depth": depth,
                "class": class_buf.value, "title": title_buf.value,
                "visible": bool(user32.IsWindowVisible(child)),
            }, ensure_ascii=False))
            walk_children(int(child), depth + 1)
            child = user32.GetWindow(child, 2)

    def enum_callback(hwnd, lparam):
        user32.GetWindowTextW(hwnd, title_buf, 512)
        user32.GetClassNameW(hwnd, class_buf, 256)
        out.append(json.dumps({
            "hwnd": int(hwnd), "parent": 0, "depth": 0,
            "class": class_buf.value, "title": title_buf.value,
            "visible": bool(user32.IsWindowVisible(hwnd)),
        }, ensure_ascii=False))
        walk_children(int(hwnd), 1)
        return True

    user32.EnumWindows(ENUM_WINDOWS_PROC(enum_callback), 0)


def phase_focused(out: list[str]) -> tuple[str, int]:
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    out.append(json.dumps({"focused_hwnd": int(hwnd), "focused_title": buf.value}, ensure_ascii=False))
    return buf.value, int(hwnd)


def phase_probe(out: list[str], step: int, x0: int, y0: int, x1: int, y1: int) -> None:
    import math
    probe_log.log_event(f"PROBE_START region=({x0},{y0})-({x1},{y1}) step={step}")
    _ensure_tree_walker()
    seen_rids: set[tuple[int, ...]] = set()
    amplitude = step * 0.4
    wavelength = step * 4.0
    phase_shift = math.pi * 2 / 3
    for y in range(y0, y1, step):
        row_idx = (y - y0) // step
        x_range = range(x1 - step, x0 - 1, -step) if row_idx % 2 else range(x0, x1, step)
        for x in x_range:
            y_actual = y + int(amplitude * math.sin(2 * math.pi * x / wavelength + row_idx * phase_shift))
            y_actual = max(y0, min(y1 - 1, y_actual))
            user32.SetCursorPos(x, y_actual)
            time.sleep(DELAY_PROBE_DWELL)
            el = element_from_point(x, y_actual)
            if not el or not el.value:
                continue
            try:
                rid = get_runtime_id(el)
                if rid and rid in seen_rids:
                    continue
                if rid:
                    seen_rids.add(rid)

                ct = get_int(el, UIA_CONTROL_TYPE)
                role = CONTROL_TYPE_MAP.get(ct, "")
                if not role:
                    continue
                name = get_str(el, UIA_NAME)
                enabled = get_bool(el, UIA_IS_ENABLED)
                offscreen = get_bool(el, UIA_IS_OFFSCREEN)

                # Walk parent chain to find containing window
                # Use RuntimeId[1] as hwnd hint (UIA encodes process hwnd there)
                wnd_name = ""
                wnd_hwnd = rid[1] if rid and len(rid) >= 2 else 0

                # Get window name from hwnd via window title
                if wnd_hwnd:
                    buf = ctypes.create_unicode_buffer(512)
                    user32.GetWindowTextW(W.HWND(wnd_hwnd), buf, 512)
                    wnd_name = buf.value

                # Count depth by walking up (capped for performance)
                depth = 0
                ancestor = el
                for _ in range(8):
                    parent = get_parent_element(ancestor)
                    if not parent or not parent.value:
                        break
                    p_hwnd = _get_hwnd_from_element(parent)
                    if p_hwnd:
                        break
                    depth += 1
                    ancestor = parent

                r = get_rect(el)
                out.append(json.dumps({
                    "probe_px": x, "probe_py": y_actual,
                    "p_role": role, "p_name": name,
                    "p_aid": get_str(el, UIA_AUTOMATION_ID), "p_desc": get_str(el, UIA_DESCRIPTION),
                    "p_x": r[0], "p_y": r[1], "p_w": r[2], "p_h": r[3],
                    "p_enabled": enabled,
                    "p_focus": get_bool(el, UIA_HAS_KEYBOARD_FOCUS),
                    "p_offscreen": offscreen,
                    "p_value": get_legacy_value(el), "p_readonly": get_legacy_readonly(el),
                    "p_depth": depth, "p_wnd": wnd_name, "p_hwnd": wnd_hwnd,
                }, ensure_ascii=False))
                probe_log.log_probe(x, y_actual, role, name, enabled, offscreen)
            except OSError:
                continue
    probe_log.log_event("PROBE_END")


def phase_windows(out: list[str], fg_title: str) -> list[tuple[ctypes.c_void_p, str, int]]:
    root = ctypes.c_void_p()
    vt(_uia, 5, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)), ctypes.byref(root))
    top_children = get_children_raw(root)
    results: list[tuple[ctypes.c_void_p, str, int]] = []
    for top_el in top_children:
        try:
            x, y, w, h = get_rect(top_el)
            ct = get_int(top_el, UIA_CONTROL_TYPE)
            role = CONTROL_TYPE_MAP.get(ct, "")
            if w <= 0 or h <= 0:
                continue
            if role not in ("Window", "Pane"):
                continue
            name = get_str(top_el, UIA_NAME)
            el_hwnd = _get_hwnd_from_element(top_el)
        except OSError:
            continue
        is_target = (name == fg_title)
        out.append(json.dumps({
            "wnd_role": role, "wnd_name": name, "wnd_hwnd": el_hwnd,
            "wnd_x": x, "wnd_y": y, "wnd_w": w, "wnd_h": h, "wnd_target": is_target,
        }, ensure_ascii=False))
        if is_target:
            results.append((top_el, name, el_hwnd))
    return results


def phase_z_order(out: list[str]) -> None:
    hwnd = user32.GetForegroundWindow()
    z_list: list[dict] = []
    buf = ctypes.create_unicode_buffer(512)
    seen: set[str] = set()
    z = 0
    while hwnd:
        if user32.IsWindowVisible(hwnd):
            user32.GetWindowTextW(hwnd, buf, 512)
            if buf.value and buf.value not in seen:
                z_list.append({"z": z, "hwnd": int(hwnd), "title": buf.value})
                seen.add(buf.value)
                z += 1
        hwnd = user32.GetWindow(hwnd, 2)
    out.append(json.dumps({"z_order": z_list}, ensure_ascii=False))


ACTIONABLE_ROLES = frozenset({
    "Button", "Edit", "ComboBox", "ListItem", "Hyperlink", "MenuItem",
    "TabItem", "SplitButton", "CheckBox", "RadioButton", "Slider",
    "Document", "Text", "ScrollBar", "TreeItem", "DataItem", "Custom",
})


def phase_tree(out: list[str], target_el, wnd_name: str, wnd_hwnd: int, timeout: float) -> None:
    probe_log.log_event(f"TREE_START wnd={wnd_name!r} hwnd={wnd_hwnd}")
    start = time.perf_counter()
    queue: deque[tuple[ctypes.c_void_p, int]] = deque()
    for child in get_children_raw(target_el):
        queue.append((child, 1))
    count = 0
    while queue:
        if time.perf_counter() - start > timeout:
            probe_log.log_event(f"TREE_TIMEOUT after {count} nodes")
            break
        raw_el, depth = queue.popleft()
        try:
            x, y, w, h = get_rect(raw_el)
            ct = get_int(raw_el, UIA_CONTROL_TYPE)
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
            name = get_str(raw_el, UIA_NAME)
            enabled = get_bool(raw_el, UIA_IS_ENABLED)
            out.append(json.dumps({
                "t_wnd": wnd_name, "t_hwnd": wnd_hwnd, "t_depth": depth,
                "t_role": role, "t_name": name,
                "t_aid": get_str(raw_el, UIA_AUTOMATION_ID), "t_desc": get_str(raw_el, UIA_DESCRIPTION),
                "t_x": x, "t_y": y, "t_w": w, "t_h": h,
                "t_enabled": enabled,
                "t_focus": get_bool(raw_el, UIA_HAS_KEYBOARD_FOCUS),
                "t_value": get_legacy_value(raw_el) if role in ACTIONABLE_ROLES else "",
                "t_readonly": get_legacy_readonly(raw_el) if role in ACTIONABLE_ROLES else False,
                "t_offscreen": get_bool(raw_el, UIA_IS_OFFSCREEN),
            }, ensure_ascii=False))
            count += 1
            probe_log.log_tree(wnd_name, depth, role, name, enabled, x, y, w, h)
        except OSError:
            continue
        try:
            for child in get_children_raw(raw_el):
                queue.append((child, depth + 1))
        except OSError:
            pass
    probe_log.log_event(f"TREE_END nodes={count}")


def phase_rescan_interacted() -> None:
    if not INTERACTION_LOG_PATH.exists():
        VERIFIED_STATE_PATH.write_text("", encoding="utf-8")
        return
    entries = [json.loads(l) for l in INTERACTION_LOG_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not entries:
        VERIFIED_STATE_PATH.write_text("", encoding="utf-8")
        return
    seen: dict[tuple[int, int], dict] = {}
    for e in entries:
        seen[(e["px"], e["py"])] = e
    results: list[str] = []
    buf = ctypes.create_unicode_buffer(512)
    for (px, py), entry in seen.items():
        el = element_from_point(px, py)
        if not el or not el.value:
            results.append(json.dumps({
                "verified": True, "status": "NOT_FOUND",
                "original_role": entry.get("role", ""), "original_name": entry.get("name", ""),
                "px": px, "py": py, "hwnd": entry.get("hwnd", 0),
            }, ensure_ascii=False))
            continue
        try:
            ct = get_int(el, UIA_CONTROL_TYPE)
            role = CONTROL_TYPE_MAP.get(ct, "")
            name = get_str(el, UIA_NAME)
            value = get_legacy_value(el)
            enabled = get_bool(el, UIA_IS_ENABLED)
            rx, ry, rw, rh = get_rect(el)
        except OSError:
            results.append(json.dumps({
                "verified": True, "status": "NOT_FOUND",
                "original_role": entry.get("role", ""), "original_name": entry.get("name", ""),
                "px": px, "py": py, "hwnd": entry.get("hwnd", 0),
            }, ensure_ascii=False))
            continue
        orig_role, orig_name = entry.get("role", ""), entry.get("name", "")
        status = "ELEMENT_CHANGED" if (role != orig_role or (orig_name and name != orig_name)) else "OK"
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
        }, ensure_ascii=False))
    VERIFIED_STATE_PATH.write_text("\n".join(results), encoding="utf-8")


def pipeline(timeout: float, probe_step: int, expand_hwnds: list[int] | None) -> list[str]:
    out: list[str] = []
    phase_dpi()
    phase_com_init()
    sw, sh = phase_screen(out)
    phase_hwnds(out)
    fg_title, fg_hwnd = phase_focused(out)
    if expand_hwnds:
        # Probe each expanded window's rectangle, not full screen
        probed_rects: set[tuple[int, int, int, int]] = set()
        for ehwnd in expand_hwnds:
            rect = W.RECT()
            if user32.GetWindowRect(W.HWND(ehwnd), ctypes.byref(rect)):
                r = (rect.left, rect.top, rect.right, rect.bottom)
                if r not in probed_rects:
                    phase_probe(out, probe_step, *r)
                    probed_rects.add(r)
        # Also probe focused window if not already covered
        rect = W.RECT()
        user32.GetWindowRect(W.HWND(fg_hwnd), ctypes.byref(rect))
        r = (rect.left, rect.top, rect.right, rect.bottom)
        if r not in probed_rects:
            phase_probe(out, probe_step, *r)
    else:
        rect = W.RECT()
        user32.GetWindowRect(W.HWND(fg_hwnd), ctypes.byref(rect))
        phase_probe(out, probe_step, rect.left, rect.top, rect.right, rect.bottom)
    phase_rescan_interacted()
    targets = phase_windows(out, fg_title)
    phase_z_order(out)
    walked_hwnds: set[int] = set()
    for target_el, wnd_name, wnd_hwnd in targets:
        phase_tree(out, target_el, wnd_name, wnd_hwnd, timeout)
        walked_hwnds.add(wnd_hwnd)
    if expand_hwnds:
        root = ctypes.c_void_p()
        vt(_uia, 5, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)), ctypes.byref(root))
        for top_el in get_children_raw(root):
            try:
                el_hwnd = _get_hwnd_from_element(top_el)
            except OSError:
                continue
            if el_hwnd in expand_hwnds and el_hwnd not in walked_hwnds:
                try:
                    name = get_str(top_el, UIA_NAME)
                except OSError:
                    name = ""
                phase_tree(out, top_el, name, el_hwnd, timeout)
                walked_hwnds.add(el_hwnd)
    return out
