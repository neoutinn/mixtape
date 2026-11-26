"""Draw the app icon (a cassette) and save .png/.ico/.icns. Needs Pillow."""

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
S = 1024


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    k = S / 64
    r = lambda *v: [x * k for x in v]
    d.rounded_rectangle(r(0, 0, 64, 64), radius=14 * k, fill="#fbe3dc")
    d.rounded_rectangle(r(6, 14, 58, 50), radius=6 * k, fill="#e0533a")
    d.rounded_rectangle(r(13, 20, 51, 35), radius=3.5 * k, fill="#fffdf9")
    for cx in (23, 41):
        d.ellipse(r(cx - 4, 23.5, cx + 4, 31.5), fill="#221d18")
    d.line(r(17, 50, 21.5, 42, 42.5, 42, 47, 50), fill="#fffdf9", width=int(3 * k), joint="curve")
    return img


if __name__ == "__main__":
    img = draw()
    img.save(HERE / "icon.png")
    img.save(HERE / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.save(HERE / "icon.icns")
    print("icons written")
