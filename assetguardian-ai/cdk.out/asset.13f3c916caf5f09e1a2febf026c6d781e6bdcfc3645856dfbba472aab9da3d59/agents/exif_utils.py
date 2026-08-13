"""Minimal, dependency-free JPEG EXIF reader.

Only stdlib (struct) is used so the Lambda package needs no Pillow/exifread
layer or Docker-based bundling to deploy. Parses just enough of the EXIF
APP1 segment to pull DateTimeOriginal, Make/Model, and GPS coordinates —
which is all the fraud-detection agent needs.
"""
import struct


def _parse_gps(gps_ifd: dict) -> tuple[float, float] | None:
    try:
        lat_ref = gps_ifd.get(1, "N")
        lat = gps_ifd.get(2)
        lon_ref = gps_ifd.get(3, "E")
        lon = gps_ifd.get(4)
        if not lat or not lon:
            return None

        def to_deg(dms):
            d, m, s = dms
            return d[0] / d[1] + (m[0] / m[1]) / 60 + (s[0] / s[1]) / 3600

        lat_deg = to_deg(lat)
        lon_deg = to_deg(lon)
        if lat_ref in (b"S", "S"):
            lat_deg = -lat_deg
        if lon_ref in (b"W", "W"):
            lon_deg = -lon_deg
        return lat_deg, lon_deg
    except Exception:
        return None


def extract_exif(jpeg_bytes: bytes) -> dict:
    """Returns {"has_exif": bool, "datetime_original": str|None,
    "camera_make": str|None, "camera_model": str|None, "gps": (lat, lon)|None}
    Best-effort; returns has_exif=False for non-JPEG or EXIF-stripped images
    (common signal for "screenshot" or "downloaded stock photo" fraud)."""
    result = {
        "has_exif": False,
        "datetime_original": None,
        "camera_make": None,
        "camera_model": None,
        "gps": None,
    }
    if jpeg_bytes[0:2] != b"\xff\xd8":
        return result

    pos = 2
    app1 = None
    while pos < len(jpeg_bytes) - 4:
        marker, length = struct.unpack(">HH", jpeg_bytes[pos : pos + 4])
        if marker == 0xFFE1:
            app1 = jpeg_bytes[pos + 4 : pos + 2 + length]
            break
        if (marker & 0xFF00) != 0xFF00:
            break
        pos += 2 + length

    if app1 is None or app1[0:6] != b"Exif\x00\x00":
        return result

    tiff = app1[6:]
    if tiff[0:2] == b"II":
        endian = "<"
    elif tiff[0:2] == b"MM":
        endian = ">"
    else:
        return result

    result["has_exif"] = True

    def read_ifd(offset):
        entries = {}
        (count,) = struct.unpack_from(endian + "H", tiff, offset)
        for i in range(count):
            entry_offset = offset + 2 + i * 12
            tag, typ, num = struct.unpack_from(endian + "HHI", tiff, entry_offset)
            val_bytes = tiff[entry_offset + 8 : entry_offset + 12]
            entries[tag] = (typ, num, val_bytes, entry_offset)
        next_offset = struct.unpack_from(
            endian + "I", tiff, offset + 2 + count * 12
        )[0]
        return entries, next_offset

    try:
        (ifd0_offset,) = struct.unpack_from(endian + "I", tiff, 4)
        ifd0, _ = read_ifd(ifd0_offset)

        if 0x010F in ifd0:
            _, num, val, _ = ifd0[0x010F]
            result["camera_make"] = tiff[
                struct.unpack(endian + "I", val)[0] : struct.unpack(endian + "I", val)[0] + num
            ].split(b"\x00")[0].decode(errors="ignore")
        if 0x0110 in ifd0:
            _, num, val, _ = ifd0[0x0110]
            result["camera_model"] = tiff[
                struct.unpack(endian + "I", val)[0] : struct.unpack(endian + "I", val)[0] + num
            ].split(b"\x00")[0].decode(errors="ignore")

        if 0x8769 in ifd0:  # Exif sub-IFD
            _, _, val, _ = ifd0[0x8769]
            exif_ifd, _ = read_ifd(struct.unpack(endian + "I", val)[0])
            if 0x9003 in exif_ifd:  # DateTimeOriginal
                _, num, val, _ = exif_ifd[0x9003]
                offset_ = struct.unpack(endian + "I", val)[0]
                result["datetime_original"] = (
                    tiff[offset_ : offset_ + num].split(b"\x00")[0].decode(errors="ignore")
                )

        if 0x8825 in ifd0:  # GPS IFD
            _, _, val, _ = ifd0[0x8825]
            gps_offset = struct.unpack(endian + "I", val)[0]
            (gps_count,) = struct.unpack_from(endian + "H", tiff, gps_offset)
            gps_ifd = {}
            for i in range(gps_count):
                entry_offset = gps_offset + 2 + i * 12
                tag, typ, num = struct.unpack_from(endian + "HHI", tiff, entry_offset)
                val_bytes = tiff[entry_offset + 8 : entry_offset + 12]
                if typ == 2:  # ASCII
                    gps_ifd[tag] = val_bytes.split(b"\x00")[0].decode(errors="ignore")
                elif typ == 5 and num == 3:  # RATIONAL x3 (DMS)
                    data_offset = struct.unpack(endian + "I", val_bytes)[0]
                    dms = []
                    for j in range(3):
                        num_, den_ = struct.unpack_from(
                            endian + "II", tiff, data_offset + j * 8
                        )
                        dms.append((num_, den_))
                    gps_ifd[tag] = dms
            result["gps"] = _parse_gps(gps_ifd)
    except Exception:
        pass

    return result
