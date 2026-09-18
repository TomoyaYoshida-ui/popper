"""pdfgen 字体层的可移植回归测试：TTC 集合的 offset 基准 + 探测必须要求 TrueType 轮廓。

这两件事原先只有「装某种系统字体」才能观察到，于是同一缺陷在 Windows 与 Linux 上都
以「找不到字体」的面目出现（run 35300378254 的 ubuntu job 即如此）。这里用合成的
最小 sfnt / 集合体把语义直接钉住，任何机器都能跑，不需要宿主有中文字体。
"""
import struct
import tempfile
import unittest
from pathlib import Path

from popper.pdfgen import _probe_cjk, load_font

SFNT_AT = 32          # 真实集合体（msyh.ttc / simsun.ttc）的第一个子字体就在 32
CJK = 0x4E2D          # '中'
CJK_GID = 1


def _pad(blob: bytes) -> bytes:
    return blob + b"\x00" * ((-len(blob)) % 4)


def _cmap_table() -> bytes:
    """单条 format 4 子表：把 CJK 这一个码位映射到 CJK_GID，外加必需的 0xFFFF 终止段。"""
    segments = [(CJK, CJK, (CJK_GID - CJK) & 0xFFFF), (0xFFFF, 0xFFFF, 1)]
    count = len(segments)
    body = struct.pack(">HHHH", count * 2, count * 2, 0, 0)
    body += b"".join(struct.pack(">H", end) for _s, end, _d in segments)
    body += struct.pack(">H", 0)                                  # reservedPad
    body += b"".join(struct.pack(">H", start) for start, _e, _d in segments)
    body += b"".join(struct.pack(">H", delta) for _s, _e, delta in segments)
    body += b"".join(struct.pack(">H", 0) for _s, _e, _d in segments)   # idRangeOffset
    subtable = struct.pack(">HHH", 4, 0, 0) + body
    subtable = subtable[:2] + struct.pack(">H", len(subtable)) + subtable[4:]
    records = struct.pack(">HHI", 3, 1, 4 + 8)                    # pid/eid/子表偏移
    return struct.pack(">HH", 0, 1) + records + subtable


def _table_bytes(tag: str) -> bytes:
    if tag == "head":
        head = bytearray(54)
        struct.pack_into(">I", head, 0, 0x00010000)
        struct.pack_into(">H", head, 18, 1000)                    # unitsPerEm
        struct.pack_into(">h", head, 50, 0)                       # indexToLocFormat
        return bytes(head)
    if tag == "maxp":
        maxp = bytearray(32)
        struct.pack_into(">I", maxp, 0, 0x00010000)
        struct.pack_into(">H", maxp, 4, 2)                        # numGlyphs
        return bytes(maxp)
    if tag == "hhea":
        hhea = bytearray(36)
        struct.pack_into(">H", hhea, 34, 2)                       # numberOfHMetrics
        return bytes(hhea)
    if tag == "hmtx":
        return b"".join(struct.pack(">HH", 500 + i, 0) for i in range(2))
    if tag == "cmap":
        return _cmap_table()
    if tag == "loca":
        return b"\x00" * 8
    if tag == "glyf":
        return b"\x00\x01\x00\x02\x00\x03\x00\x04"
    if tag == "CFF ":
        return b"postscript outlines, no glyf"
    raise AssertionError(f"未登记的测试表 {tag}")


def build_sfnt(tags=("head", "maxp", "cmap", "hhea", "hmtx", "loca", "glyf"),
               base: int = 0) -> bytes:
    """拼一个最小但可解析的 sfnt；`base` 把整块内容平移到集合体深处。

    表目录里的 offset 写成 `base + 位置`，即**以文件开头为基准**——这正是真实 TTC
    的做法（多个子字体共享同一批表也依赖它），也是 `load_font` 曾搞错的地方。
    """
    blobs = [(tag, _pad(_table_bytes(tag))) for tag in tags]
    dir_size = 12 + 16 * len(blobs)
    cursor = base + dir_size
    offsets, parts = [], []
    for _tag, blob in blobs:
        offsets.append(cursor)
        parts.append(blob)
        cursor += len(blob)
    header = struct.pack(">IHHHH", 0x00010000, len(blobs), 0, 0, 0)
    directory = b"".join(struct.pack(">4sIII", tag.encode("latin1"), 0, offset, len(blob))
                         for (tag, blob), offset in zip(blobs, offsets))
    body = b"".join(parts)
    return b"\x00" * base + header + directory + body


def build_collection(sfnt: bytes) -> bytes:
    """TTC v2 集合头（真实 msyh.ttc/simsun.ttc 就是这个版本），子字体放在 SFNT_AT。"""
    header = b"ttcf" + struct.pack(">II", 0x00020000, 1) + struct.pack(">I", SFNT_AT)
    header += struct.pack(">II", 0, 0)                            # v2 的 DSIG 两个字段
    header += b"\x00" * (SFNT_AT - len(header))                   # 真实集合体也补到 4 字节边界
    assert len(header) == SFNT_AT, "集合头必须正好填到子字体起始处"
    # sfnt 的前 SFNT_AT 字节是 build_sfnt(base=SFNT_AT) 留的空洞，在这里换成集合头
    return header + sfnt[len(header):]


class TtcCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def _write(self, name: str, data: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_standalone_sfnt_is_readable(self):
        path = self._write("bare.ttf", build_sfnt())
        font = load_font(path)
        self.assertEqual(CJK_GID, font.glyph_map.get(CJK))
        self.assertTrue(_probe_cjk(path))

    def test_collection_offsets_are_absolute_from_file_start(self):
        path = self._write("collection.ttc", build_collection(build_sfnt(base=SFNT_AT)))
        font = load_font(path)
        # 子字体不在文件开头：偏移必须整体大于 SFNT_AT，否则这份 fixture 没在测同一件事
        self.assertGreater(font.tables["cmap"][0], SFNT_AT)
        self.assertEqual(CJK_GID, font.glyph_map.get(CJK))
        self.assertTrue(_probe_cjk(path))

    def test_broken_collection_header_is_rejected_not_misparsed(self):
        # 头里声称子字体在 8，但那里没有 sfnt：必须报错，不能悄悄解析出个错字体
        header = b"ttcf" + struct.pack(">II", 0x00020000, 1) + struct.pack(">I", 8)
        path = self._write("junk.ttc", header + b"\x00" * 64)
        with self.assertRaises(Exception):
            load_font(path)


class FontProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def test_cff_outlines_are_rejected_even_with_cjk_mapping(self):
        """子集化器按字节读 glyf/loca，CFF 字体（如 Debian 的 fonts-noto-cjk）能过
        cmap 探测却会在渲染中途炸，所以探测必须同时要求 TrueType 轮廓。"""
        path = self.dir / "cff.otf"
        path.write_bytes(build_sfnt(tags=("head", "maxp", "cmap", "hhea", "hmtx", "CFF ")))
        font = load_font(path)
        self.assertEqual(CJK_GID, font.glyph_map.get(CJK))     # cmap 探测这一关会过
        self.assertFalse(_probe_cjk(path))                     # 但轮廓不是 TrueType

    def test_true_font_without_cjk_mapping_is_rejected(self):
        """名字像中文字体不等于有常用汉字（simsunb.ttf 只含增补区）。"""
        data = build_sfnt()
        # 把 cmap 子表里的映射改成只覆盖另一个码位，模拟「无常用汉字」
        # （用固定名字而不是 mkstemp：它会漏一个没人关的 fd，目录先被清掉）
        path = self.dir / "no-cjk.ttf"
        path.write_bytes(data.replace(struct.pack(">H", CJK), struct.pack(">H", 0x3042)))
        self.assertFalse(_probe_cjk(path))


if __name__ == "__main__":
    unittest.main()
