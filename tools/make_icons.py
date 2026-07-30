#!/usr/bin/env python3
"""Render the menu bar status icons.

A roundel-style mark tinted by state, drawn with Cocoa so it is properly
anti-aliased at menu bar size. Uses PyObjC, which rumps already depends on, so
no extra requirement.

Deliberately NOT the BMW roundel: that is a registered trademark and this
repository is public. Same silhouette family, our own mark.

    python tools/make_icons.py     -> assets/icons/*.png
"""

from pathlib import Path

from AppKit import (
    NSBezierPath,
    NSBitmapImageRep,
    NSColor,
    NSGraphicsContext,
    NSMakeRect,
)

OUT = Path(__file__).resolve().parent.parent / "assets" / "icons"
SIZE = 44  # 22pt at 2x

# Status colours, close to the macOS system palette so they sit naturally in the
# menu bar next to everything else.
STATES = {
    "green": (0.20, 0.78, 0.35),   # streaming, heard from recently
    "yellow": (1.00, 0.80, 0.00),  # up, but the car has been quiet
    "orange": (1.00, 0.58, 0.00),  # database unreachable
    "red": (1.00, 0.23, 0.19),     # stream down
    "grey": (0.56, 0.56, 0.58),    # unknown / not set up
}


def _rgb(rgb, alpha=1.0):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(*rgb, alpha)


def draw(rgb) -> bytes:
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, SIZE, SIZE, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0
    )
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    ctx.setShouldAntialias_(True)

    inset = 3.0
    outer = NSMakeRect(inset, inset, SIZE - 2 * inset, SIZE - 2 * inset)

    # Solid disc in the status colour.
    _rgb(rgb).setFill()
    NSBezierPath.bezierPathWithOvalInRect_(outer).fill()

    # Inner disc, leaving a ring.
    ring = 5.0
    inner_rect = NSMakeRect(
        outer.origin.x + ring,
        outer.origin.y + ring,
        outer.size.width - 2 * ring,
        outer.size.height - 2 * ring,
    )
    NSColor.whiteColor().setFill()
    NSBezierPath.bezierPathWithOvalInRect_(inner_rect).fill()

    # Two opposite quadrants filled back in, giving the quartered look that
    # reads as "car" at 22 points without being anyone's trademark.
    cx = inner_rect.origin.x + inner_rect.size.width / 2
    cy = inner_rect.origin.y + inner_rect.size.height / 2
    radius = inner_rect.size.width / 2
    _rgb(rgb).setFill()
    for start in (90, 270):
        wedge = NSBezierPath.bezierPath()
        wedge.moveToPoint_((cx, cy))
        wedge.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
            (cx, cy), radius, start, start + 90
        )
        wedge.closePath()
        wedge.fill()

    NSGraphicsContext.restoreGraphicsState()
    return rep.representationUsingType_properties_(4, None)  # 4 = PNG


def main() -> None:
    global SIZE
    OUT.mkdir(parents=True, exist_ok=True)
    for name, rgb in STATES.items():
        data = draw(rgb)
        path = OUT / f"status-{name}.png"
        path.write_bytes(bytes(data))
        print(f"wrote {path}")

    # Application icon. Not shown in the Dock -- the app runs as an accessory --
    # but notifications and the app switcher fall back to it, and without one
    # they show the generic Python rocket.
    SIZE = 512
    (OUT / "app.png").write_bytes(bytes(draw(STATES["green"])))
    print(f"wrote {OUT / 'app.png'}")


if __name__ == "__main__":
    main()
