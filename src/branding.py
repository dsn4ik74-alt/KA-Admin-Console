"""Native vector geometry for the KA application icon; no external artwork."""
import math
from pathlib import Path
import struct
import tkinter as tk
from functools import lru_cache


@lru_cache(maxsize=4)
def pixels(size):
    segments = ((.23, .25, .23, .75), (.23, .5, .43, .25), (.23, .5, .44, .75),
                (.54, .75, .68, .25), (.68, .25, .84, .75), (.59, .57, .78, .57))
    def distance(x, y, x1, y1, x2, y2):
        factor = max(0, min(1, ((x-x1)*(x2-x1)+(y-y1)*(y2-y1))/((x2-x1)**2+(y2-y1)**2)))
        return math.hypot(x-x1-factor*(x2-x1), y-y1-factor*(y2-y1))
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            samples = []
            for dy, dx in ((.25, .25), (.25, .75), (.75, .25), (.75, .75)):
                x, y = (px+dx)/size, (py+dy)/size
                outside = math.hypot(max(abs(x-.5)-.31, 0), max(abs(y-.5)-.31, 0)) > .17
                if outside:
                    samples.append((0, 0, 0, 0))
                elif any(distance(x, y, *segment) < .034 for segment in segments):
                    samples.append((255, 255, 255, 255))
                else:
                    samples.append((round(64-25*y), round(115-36*y), round(230-25*y), 255))
            alpha = sum(sample[3] for sample in samples) // 4
            solid = [sample for sample in samples if sample[3]]
            row.append(tuple(round(sum(sample[channel] for sample in solid)/len(solid)) if solid else 0
                             for channel in range(3)) + (alpha,))
        rows.append(row)
    return rows


def tk_icon(root):
    icon = tk.PhotoImage(master=root, width=32, height=32)
    for y, row in enumerate(pixels(32)):
        icon.put('{' + ' '.join('#%02x%02x%02x' % pixel[:3] for pixel in row) + '}', to=(0, y))
        for x, pixel in enumerate(row):
            if pixel[3] < 128:
                icon.transparency_set(x, y, True)
    return icon


def write_icon(path):
    entries, images = [], []
    offset = 6 + 16 * 4
    for size in (16, 32, 48, 256):
        rows = pixels(size)
        bitmap = b''.join(bytes((blue, green, red, alpha)) for row in reversed(rows) for red, green, blue, alpha in row)
        mask = bytes(((size + 31) // 32) * 4 * size)
        dib = struct.pack('<IiiHHIIiiII', 40, size, size * 2, 1, 32, 0, len(bitmap), 0, 0, 0, 0) + bitmap + mask
        entries.append(struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(dib), offset))
        images.append(dib)
        offset += len(dib)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack('<HHH', 0, 1, 4) + b''.join(entries) + b''.join(images))
