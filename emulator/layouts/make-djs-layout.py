#!/usr/bin/env python3
"""Generate panel-layout-djs-1.22.json: a neutral DJS-1000 top panel for the emulator window.

Positions were measured by eye from a top-panel product photo (860 x 981 px; panel area from x 85, y 40) and are
written in canvas units = 2 x photo pixels. Nothing from the photo is reproduced except coordinates; drawing uses the
emulator's own control styles. Control ids 1..89 keep their SP-16 meaning (panel_map.CONTROL_NAMES); 90+ are the
DJS-only controls. Run from the repository root: python3 emulator/layouts/make-djs-layout.py
"""
import json
from pathlib import Path

OX, OY, S = 85, 40, 2  # photo origin of the panel area and the scale to canvas units


def P(x, y):
    return round((x - OX) * S, 1), round((y - OY) * S, 1)


controls, decor = [], []


def control(cid, name, kind, group, shape, x, y, access, **extra):
    cx, cy = P(x, y)
    item = {"id": cid, "name": name, "kind": kind, "group": group, "shape": shape, "cx": cx, "cy": cy,
            "accessibility": access}
    for key in ("w", "h", "r", "capR"):
        if key in extra:
            extra[key] = round(extra[key] * S, 1)
    item.update(extra)
    controls.append(item)


def text(label, x, y, size=13, weight="semibold", align="center", color="white"):
    tx, ty = P(x, y)
    decor.append({"type": "text", "text": label, "x": tx, "y": ty, "size": size, "weight": weight, "align": align,
                  "color": color})


def box(x, y, w, h, radius=1, line=1.6):
    bx, by = P(x, y)
    decor.append({"type": "box", "x": bx, "y": by, "w": w * S, "h": h * S, "lineWidth": line, "radius": radius})


def dots(x, y, r, count=6, size=3.2):
    cx, cy = P(x, y)
    decor.append({"type": "dots", "cx": cx, "cy": cy, "r": r * S, "from": -120, "to": 120, "count": count, "size": size})


def button(cid, name, label, x, y, w=60, h=15, style="white", access=None, group="buttons"):
    control(cid, name, "button", group, "roundRect", x, y, access or label.title(), label=label, style=style,
            radius=5, w=w, h=h)


# --- raised screen housing (drawn first, behind the LCD and the PROJECT/SAMPLING/FX column) ---
hx, hy = P(170, 42)
decor.append({"type": "insert", "x": hx, "y": hy, "w": (672 - 170) * S, "h": (272 - 42) * S, "radius": 8})

# --- pads: 4 x 4, pad 1 bottom left (the SP-16 numbering) ---
for i in range(16):
    col, row = i % 4, i // 4
    control(i + 1, f"pad{i + 1}", "pad", "pads", "roundRect", 302 + col * 78.7, 670 - row * 78.7, f"Pad {i + 1}",
            number=str(i + 1), numberDy=-76, numberAlign="left", radius=9, w=64, h=64)
# --- step keys: two rows of eight ---
for i in range(16):
    col, row = i % 8, i // 8
    control(27 + i, f"step{i + 1}", "step", "steps", "roundRect", 273 + col * 42.2, 772 + row * 80, f"Step {i + 1}",
            number=str(i + 1), numberDy=-60, numberAlign="center", numberBox=(i % 4 == 0), radius=6, w=31, h=44)
# --- bar keys 1..4 with their indicator lamps ---
for i in range(4):
    control(43 + i, f"bar{i + 1}", "bar", "bars", "roundRect", 628 + i * 32, 857, f"Bar {i + 1}",
            number=str(i + 1), numberDy=-74, numberAlign="center", radius=5, w=24, h=30)
    control(84 + i, f"bar_led{i + 1}", "lamp", "bars", "roundRect", 628 + i * 32, 829, f"Bar {i + 1} indicator",
            radius=2, w=22, h=3.5)
# --- parameter encoders and selector ---
for i in range(6):
    control(47 + i, f"knob{i + 1}", "knob", "knobs", "circle", 280 + i * 55.8, 316, f"Encoder {i + 1}", r=18)
control(53, "selector", "selector", "selector", "circle", 630, 176, "Selector", r=30, capR=19)
# selector: LED ring of small segments in the recess, and the raised lip around it
sx, sy = P(630, 176)
decor.append({"type": "dots", "cx": sx, "cy": sy, "r": 34 * S, "from": 0, "to": 352.5, "count": 48, "size": 2.4,
              "color": "gray"})
decor.append({"type": "dots", "cx": sx, "cy": sy, "r": 38 * S, "from": 0, "to": 354, "count": 60, "size": 0, "tick": 3,
              "color": "dark"})
# --- buttons ---
button(18, "mute", "MUTE", 302, 363)
button(90, "hot_slice", "HOT SLICE", 381, 363, access="Hot slice")
button(19, "slice", "SLICE", 459, 363)
button(20, "scale", "SCALE", 538, 363)
button(91, "project", "PROJECT", 209, 108, w=46, h=15, style="mode")
button(92, "sampling", "SAMPLING", 209, 162, w=46, h=15, style="mode")
button(93, "fx_select", "FX", 209, 198, w=46, h=15, style="mode", access="FX select")
button(21, "shift", "SHIFT", 165, 653, w=36, h=13, style="gray")
button(25, "scene", "SCENE", 639, 773, w=40, h=15)
button(26, "pattern", "PATTERN", 711, 773, w=42, h=15)
button(56, "mode", "MODE", 148, 407, w=40, h=17, style="mode", access="Ribbon mode")
button(99, "sync", "SYNC", 655, 316, w=24, h=15, access="Beat sync")
button(100, "sync_master", "MASTER", 691, 316, w=24, h=15, access="Beat sync master")
button(95, "nudge_minus", "−", 651, 703, w=30, h=17, access="Nudge minus")
button(96, "nudge_plus", "+", 699, 703, w=30, h=17, access="Nudge plus")
control(22, "rec", "button", "transport", "roundRect", 165, 703, "Record", label="", style="rec", radius=26, w=26, h=26)
control(23, "stop", "button", "transport", "roundRect", 167, 773, "Stop/Gate", label="", style="stop", radius=60, w=60, h=60)
control(24, "play", "button", "transport", "roundRect", 167, 853, "Play/Pause", label="", style="play", radius=60, w=60, h=60)
control(54, "back", "button", "nav", "pill", 614, 108, "Back", style="back", radius=7, w=30, h=13)
control(55, "home", "button", "nav", "circle", 654, 106, "Utility/Home", style="home", r=8)
control(59, "usb_stop", "button", "usb", "circle", 120, 196, "USB stop", style="round", r=10)
control(57, "hold", "button", "mode", "circle", 225, 530, "Ribbon hold", style="home", r=8)
control(94, "fx_on_off", "button", "fx", "circle", 200, 340, "FX on/off", style="home", r=20)
control(97, "tempo_reset", "button", "tempo", "circle", 614, 530, "Tempo reset", style="home", r=9)
control(98, "tempo_range", "button", "tempo", "circle", 676, 366, "Tempo range", style="home", r=8)
# --- analog: FX level/depth (wired), input level (pure analog on the unit: no RX field) ---
control(101, "fx_level_depth", "analog", "analog", "circle", 130, 340, "FX level/depth", style="matte", r=17)
control(103, "input_level", "analog", "analog", "circle", 715, 130, "Input level", style="metal", r=14)
dots(130, 340, 23)
dots(715, 130, 19)
# --- ribbon (the CDC strip) with its LEDs and the mode lamps ---
control(58, "strip", "strip", "strip", "roundRect", 160, 530, "Ribbon", radius=4, w=40, h=180)
# eight ribbon LEDs (TX lamp fields 0x63-0x64, bottom to top; profile strip_led1-8)
for i in range(8):
    control(70 + i, f"strip_led{i + 1}", "lamp", "strip", "roundRect", 186, 607 - i * 21.7, f"Ribbon LED {i + 1}",
            radius=2, w=4, h=14)
for i, name in enumerate(("pitch", "repeat", "user1", "user2")):
    control(80 + i, f"lamp_{name}", "lamp", "mode", "text", 190, 393 + i * 9.7, f"Mode lamp {name.title()}",
            label=name.upper(), w=30, h=9)

# --- printed labels ---
for label, x, y in (("USB STOP", 120, 180), ("FX", 155, 299), ("LEVEL/DEPTH", 130, 312), ("ON/OFF", 200, 312),
                    ("HOLD", 225, 515), ("REC / UNDO", 165, 683), ("STOP/GATE", 167, 733), ("PLAY/PAUSE", 167, 812),
                    ("BACK", 614, 93), ("UTILITY/HOME", 654, 90), ("INPUT LEVEL", 715, 112), ("BEAT SYNC", 673, 293),
                    ("TEMPO", 676, 344), ("±6/±10/±16/WIDE", 676, 352), ("TEMPO", 614, 505), ("RESET", 614, 514),
                    ("TEMPO", 676, 664), ("NUDGE", 675, 686), ("LENGTH", 711, 790)):
    text(label, x, y, size=11)
decor.append({"type": "line", "x": P(150, 300)[0] - 40, "y": P(150, 300)[1], "x2": P(150, 300)[0] - 12,
              "y2": P(150, 300)[1], "lineWidth": 1.6})
# details from the numbered top-panel drawing: knob scales, sub-labels, ribbon scale, USB port
for label, x, y in (("MIN", 113, 364), ("MAX", 148, 364), ("MIN", 701, 152), ("MAX", 730, 152), ("SET", 621, 547),
                    ("SCENE JUMP", 645, 790), ("CLEAR SEQ.", 711, 757), ("5V ⎓ 1A", 120, 155)):
    text(label, x, y, size=10)
for label, y in (("+", 447), ("1/8", 476), ("1/4", 519), ("1/2", 556), ("1/1", 594), ("−", 616)):
    text(label, 204, y, size=10)
box(686, 750, 50, 14, radius=1, line=1.4)  # CLEAR SEQ. frame
box(637, 116, 34, 10, radius=1, line=1.4)  # WAKE UP frame
text("WAKE UP", 654, 121, size=9)
ux, uy = P(100, 112)
decor.append({"type": "usbport", "x": ux, "y": uy, "w": 40 * S, "h": 36 * S, "radius": 6})
# the port's white ring (TX lamp field 0x69 f0, profile usb_led): lit while a stick is mounted, drawn as a bar under it
control(88, "usb_led", "lamp", "usb", "roundRect", 120, 150, "USB port ring", radius=2, w=36, h=3)
# tempo slider: an analog control of shape "fader" (profile analog word tempo_slider; top = minus), a slot with fine
# increments (longer every fifth), centre mark and scale
control(104, "tempo_slider", "analog", "tempo", "fader", 675, 530, "Tempo slider", radius=3, w=14, h=250)
for i in range(41):
    y = 410 + i * 6
    long = i % 5 == 0
    x0, y0 = P(664 - (5 if long else 2.5), y)
    x1, y1 = P(664, y)
    decor.append({"type": "line", "x": x0, "y": y0, "x2": x1, "y2": y1, "lineWidth": 1.6 if long else 1, "color": "gray"})
cx0, cy0 = P(683, 530)
cx1, cy1 = P(690, 530)
decor.append({"type": "line", "x": cx0, "y": cy0, "x2": cx1, "y2": cy1, "lineWidth": 2})
for label, y in (("−", 410), ("0", 530), ("+", 640)):
    text(label, 656, y, size=10)

# front edge (photos of a unit): PHONES on a 1/4" and a 3.5 mm jack, and the headphone LEVEL knob, which the firmware
# reads as "Headphone Level" (profile analog word headphone_level)
decor.append({"type": "line", "x": P(85, 912)[0], "y": P(85, 912)[1], "x2": P(760, 912)[0], "y2": P(760, 912)[1],
              "lineWidth": 2, "color": "gray"})
text("PHONES", 112, 928, size=11)
for x, size in ((104, 22), (124, 12)):
    jx, jy = P(x, 942)
    decor.append({"type": "dots", "cx": jx, "cy": jy, "r": 0, "from": 0, "to": 0, "count": 1, "size": size})
decor.append({"type": "line", "x": P(110, 942)[0], "y": P(110, 942)[1], "x2": P(119, 942)[0], "y2": P(119, 942)[1],
              "lineWidth": 1.4})
control(102, "headphone_level", "analog", "analog", "circle", 160, 942, "Headphone level", style="metal", r=12)
dots(160, 942, 17)
text("LEVEL", 160, 924, size=10)
text("FRONT", 735, 942, size=9, color="gray")

bezel_x, bezel_y = P(245, 66)
active_x, active_y = P(258, 78)
layout = {
    "version": 1,
    "canvas": {"width": (760 - OX) * S, "height": (965 - OY) * S, "cornerRadius": 5, "railWidth": 25,
               "plinthTop": (955 - OY) * S},
    "lcd": {"bezel": {"x": bezel_x, "y": bezel_y, "w": 346 * S, "h": 216 * S, "radius": 7},
            "active": {"x": active_x, "y": active_y, "w": 320 * S, "h": 192 * S},
            "framebuffer": {"width": 800, "height": 480}},
    "controls": sorted(controls, key=lambda c: c["id"]),
    "decor": decor,
}
ids = [c["id"] for c in controls]
assert len(ids) == len(set(ids)), "duplicate control id"
out = Path(__file__).resolve().parents[1] / "panel-layout-djs-1.22.json"
out.write_text(json.dumps(layout, indent=1, ensure_ascii=False) + "\n")
print(f"wrote {out.name}: {len(controls)} controls, {len(decor)} decor items")
