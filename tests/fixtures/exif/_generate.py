"""tests/fixtures/exif/_generate.py — 生成 EXIF 測試樣本。

執行：python tests/fixtures/exif/_generate.py
（需在 ivy-backend 根目錄）

產出：
- with_gps.jpg: 100x100 RGB JPEG，含 GPS EXIF（台北 25.0339°N, 121.5645°E）+ Make/Model
- with_orientation_6.jpg: 100x100 RGB JPEG，Orientation=6（CW 90° rotate）
- clean.png: 50x50 PNG 無 metadata
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from PIL.ExifTags import Base as ExifBase
from PIL.TiffImagePlugin import IFDRational

HERE = Path(__file__).parent


def _gps_ifd():
    """構造 GPS IFD：台北 25.0339°N, 121.5645°E"""

    def to_dms_rational(deg: float):
        d = int(deg)
        m = int((deg - d) * 60)
        s = (deg - d - m / 60) * 3600
        return (
            IFDRational(d, 1),
            IFDRational(m, 1),
            IFDRational(int(s * 10000), 10000),
        )

    return {
        1: "N",  # GPSLatitudeRef
        2: to_dms_rational(25.0339),  # GPSLatitude
        3: "E",  # GPSLongitudeRef
        4: to_dms_rational(121.5645),  # GPSLongitude
        5: b"\x00",  # GPSAltitudeRef
        6: IFDRational(10, 1),  # GPSAltitude
    }


def gen_with_gps():
    img = Image.new("RGB", (100, 100), color=(120, 180, 200))
    exif = img.getexif()
    exif[ExifBase.Make.value] = "Apple"
    exif[ExifBase.Model.value] = "iPhone 14 Pro"
    exif[ExifBase.Software.value] = "iOS 17.2"
    exif[ExifBase.DateTime.value] = "2026:05:28 14:30:00"

    # GPS IFD 透過 get_ifd 拿到子 IFD 寫入
    gps_ifd = exif.get_ifd(ExifBase.GPSInfo.value)
    for k, v in _gps_ifd().items():
        gps_ifd[k] = v

    out = HERE / "with_gps.jpg"
    img.save(out, format="JPEG", exif=exif, quality=90)
    print(f"wrote {out}")


def gen_with_orientation_6():
    img = Image.new("RGB", (100, 60), color=(255, 200, 100))  # 橫長 100x60
    exif = img.getexif()
    exif[ExifBase.Orientation.value] = 6  # CW 90° rotate to view
    out = HERE / "with_orientation_6.jpg"
    img.save(out, format="JPEG", exif=exif, quality=90)
    print(f"wrote {out}")


def gen_clean_png():
    img = Image.new("RGB", (50, 50), color=(50, 50, 50))
    out = HERE / "clean.png"
    img.save(out, format="PNG", optimize=False)
    print(f"wrote {out}")


if __name__ == "__main__":
    gen_with_gps()
    gen_with_orientation_6()
    gen_clean_png()
