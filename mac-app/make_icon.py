"""Draw the app icon (a film frame with an aperture) as a 1024 px PNG."""
import math
import sys

from PIL import Image, ImageDraw

S = 1024
im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
pad = 90
d.rounded_rectangle((pad, pad, S - pad, S - pad), radius=190, fill=(28, 28, 30, 255))
# film strip: sprocket holes top and bottom
for y0 in (pad + 70, S - pad - 130):
    for k in range(7):
        x = pad + 75 + k * 102
        d.rounded_rectangle((x, y0, x + 56, y0 + 60), radius=12, fill=(70, 70, 74, 255))
# the frame
fx0, fy0, fx1, fy1 = pad + 70, pad + 175, S - pad - 70, S - pad - 175
d.rounded_rectangle((fx0, fy0, fx1, fy1), radius=36, fill=(232, 188, 92, 255))
# aperture blades
cx, cy, r = S / 2, (fy0 + fy1) / 2, (fy1 - fy0) * 0.36
d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(28, 28, 30, 255))
for k in range(6):
    a = math.radians(k * 60 + 15)
    p1 = (cx + r * 0.42 * math.cos(a), cy + r * 0.42 * math.sin(a))
    p2 = (cx + r * math.cos(a + 0.9), cy + r * math.sin(a + 0.9))
    d.line((p1, p2), fill=(232, 188, 92, 255), width=14)
d.ellipse((cx - r * 0.42, cy - r * 0.42, cx + r * 0.42, cy + r * 0.42), outline=(232, 188, 92, 255), width=14)
im.save(sys.argv[1])
