"""PDF 稿件渲染器。仅用标准库生成合法 PDF，并内嵌 TrueType 中文字体实现真实渲染。

不依赖 reportlab 等第三方库。渲染时发现系统 CJK 字体并以 CIDFontType2(Identity-H)
全量内嵌；中文与西文均按字体 advance 宽度排版与换行，带 ToUnicode 便于文本抽取。
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

from .core import ProtocolError

# 页面几何（A4 / 磅）
PAGE_W = 595.28
PAGE_H = 841.89
MARGIN = 60.0
LINE_GAP = 1.5
SIZES = {"title": 18, "label": 14, "section": 14, "body": 11, "note": 10}


# ---- TrueType 解析 ---------------------------------------------------------
class _TTFont:
    def __init__(self, raw: bytes):
        self.data = raw
        if len(raw) < 12 or struct.unpack_from(">I", raw, 0)[0] != 0x00010000:
            raise ProtocolError("非 TrueType 字体（缺少 0x00010000 scaler），请提供 .ttf 文件")
        num_tables = struct.unpack_from(">H", raw, 4)[0]
        dir_off = 12
        self.tables: dict[str, tuple[int, int]] = {}
        for _ in range(num_tables):
            tag = raw[dir_off:dir_off + 4].decode("latin1")
            off, size = struct.unpack_from(">II", raw, dir_off + 8)
            self.tables[tag] = (off, size)
            dir_off += 16
        self._parse()

    def _slice(self, tag: str) -> bytes:
        off, size = self.tables[tag]
        return self.data[off:off + size]

    def _parse(self):
        for tag in ("head", "maxp", "cmap", "hhea", "hmtx"):
            if tag not in self.tables:
                raise ProtocolError(f"字体缺少 {tag} 表")
        head = self._slice("head")
        self.units_per_em = struct.unpack_from(">H", head, 18)[0] or 1000
        self.num_glyphs = struct.unpack_from(">H", self._slice("maxp"), 4)[0]
        self.index_to_loc = struct.unpack_from(">H", head, 50)[0]
        self._parse_cmap()
        self._parse_hmtx()
        self.ps_name = self._ps_name("PopperEmbedded")

    def _parse_cmap(self):
        cmap = self._slice("cmap")
        num = struct.unpack_from(">H", cmap, 2)[0]
        format4 = format12 = None
        for i in range(num):
            pid, eid, off = struct.unpack_from(">HHI", cmap, 4 + i * 8)
            if off + 2 > len(cmap):
                continue
            fmt = struct.unpack_from(">H", cmap, off)[0]
            sub = cmap[off:]
            if fmt == 12 and (pid == 3 and eid == 10 or pid == 0):
                format12 = self._parse_cmap12(sub)
            elif fmt == 4 and (pid == 3 and eid == 1 or pid == 0):
                format4 = self._parse_cmap4(sub)
        self.glyph_map = format12 or format4
        if self.glyph_map is None:
            raise ProtocolError("字体缺少可用 Unicode 编码表（cmap format 4/12）")

    def _parse_cmap4(self, sub: bytes) -> dict[int, int]:
        seg_count = struct.unpack_from(">H", sub, 6)[0] // 2
        end = struct.unpack_from(f">{seg_count}H", sub, 14)
        start_off = 14 + seg_count * 2 + 2
        start = struct.unpack_from(f">{seg_count}H", sub, start_off)
        delta_off = start_off + seg_count * 2
        delta_raw = struct.unpack_from(f">{seg_count}H", sub, delta_off)
        delta = [d if d < 0x8000 else d - 0x10000 for d in delta_raw]
        ro_off = delta_off + seg_count * 2
        ro = struct.unpack_from(f">{seg_count}H", sub, ro_off)
        idarray_off = ro_off + seg_count * 2
        result: dict[int, int] = {}
        for i in range(seg_count):
            if start[i] == 0xFFFF:
                continue
            for c in range(start[i], min(end[i], 0xFFFF) + 1):
                gid = 0
                if ro[i] == 0:
                    gid = (c + delta[i]) & 0xFFFF
                else:
                    addr = idarray_off + ro[i] + 2 * (c - start[i])
                    if addr + 2 <= len(sub):
                        gid = struct.unpack_from(">H", sub, addr)[0]
                    if gid:
                        gid = (gid + delta[i]) & 0xFFFF
                if gid:
                    result[c] = gid
        return result

    def _parse_cmap12(self, sub: bytes) -> dict[int, int]:
        n = struct.unpack_from(">I", sub, 12)[0]
        result: dict[int, int] = {}
        for i in range(n):
            s, e, g = struct.unpack_from(">III", sub, 16 + i * 12)
            for c in range(s, min(e, 0x10FFFF) + 1):
                result[c] = g + (c - s)
        return result

    def _parse_hmtx(self):
        num_h = struct.unpack_from(">H", self._slice("hhea"), 34)[0]
        hmtx = self._slice("hmtx")
        self.widths: dict[int, int] = {}
        for i in range(num_h):
            self.widths[i] = struct.unpack_from(">H", hmtx, i * 4)[0]
        if num_h:
            default = self.widths[num_h - 1]
            for i in range(num_h, self.num_glyphs):
                self.widths[i] = default
        self.default_width = self.widths.get(ord("中"), self.widths.get(0, 1000))

    def _ps_name(self, fallback: str) -> str:
        if "name" not in self.tables:
            return fallback
        name = self._slice("name")
        count = struct.unpack_from(">H", name, 2)[0]
        str_off = struct.unpack_from(">H", name, 4)[0]
        for i in range(count):
            pid, _, _, nid, length, off = struct.unpack_from(">HHHHHH", name, 6 + i * 12)
            if nid != 6 or pid not in (3, 1):
                continue
            raw = name[str_off + off:str_off + off + length]
            try:
                best = raw.decode("utf-16-be" if pid == 3 else "mac_roman")
            except Exception:
                continue
            safe = "".join(ch for ch in best if ch.isalnum() or ch in "-_").strip() or fallback
            return safe[:63]
        return fallback

    def width_units(self, gid: int) -> int:
        return self.widths.get(gid, self.default_width)


def load_font(font_path: str | Path) -> _TTFont:
    """读取字体文件；自动剥离 TTC 集合头取首个字体子集。"""
    data = Path(font_path).read_bytes()
    if data[:4] == b"ttcf":
        off = struct.unpack_from(">I", data, 12)[0]
        data = data[off:]
    return _TTFont(data)


def discover_font() -> str | None:
    """在常见系统字体路径中寻找一个可用的 TrueType 中文字体，并实际探测其含常用 CJK 字形。

    仅靠文件名不足以判断（如 simsunb.ttf 是 SimSun-ExtB，只含增补区字形），
    需用 cmap 校验一个常见汉字（'中' U+4E2D）能否映射到真实 glyph。返回路径或 None。
    """
    if os.name == "nt":  # Windows
        windir = os.environ.get("WINDIR") or "C:/Windows"
        base = Path(windir) / "Fonts"
        names = ["simhei.ttf", "simkai.ttf", "simfang.ttf", "Deng.ttf",
                 "msyh.ttc", "simsun.ttc", "msyhl.ttc",
                 "simsunb.ttf", "NotoSerifSC-VF.ttf", "NotoSansSC-VF.ttf"]
        candidates = [base / n for n in names]
    else:
        base = Path("/usr/share/fonts")
        if base.is_dir():
            candidates = [p for p in sorted(base.rglob("*"))
                          if p.suffix.lower() in (".ttf", ".ttc")
                          and any(q in p.name.lower()
                                  for q in ("cjk", "chinese", "noto", "wqy", "hans", "sc",
                                            "simhei", "simkai", "simfang", "msyh", "deng"))]
        else:
            candidates = []
    for p in candidates:
        try:
            if not p.is_file():
                continue
            if _probe_cjk(p):
                return str(p)
        except Exception:
            continue
    # Windows 下直接遍历字体目录作为兜底
    if os.name == "nt" and base.is_dir():
        for p in sorted(base.glob("*.tt*")):
            try:
                if _probe_cjk(p):
                    return str(p)
            except Exception:
                continue
    return None


def _probe_cjk(font_path: str | Path) -> bool:
    """加载字体并确认常见汉字（'中' 0x4E2D）在其中的映射不为空。"""
    font = load_font(font_path)
    gid = font.glyph_map.get(0x4E2D, 0)
    return bool(gid)


# ---- PDF 结构生成 ----------------------------------------------------------
def _build_cmap_format4(mapping: dict[int, int]) -> bytes:
    """构建 cmap format 4 子表（单字符段），映射 Unicode(BMP)→glyph。"""
    items = sorted(mapping.items())
    seg_count = len(items)
    seg2 = seg_count * 2
    search_range = 2
    entry_selector = 0
    while search_range * 2 <= seg2:
        search_range *= 2
        entry_selector += 1
    range_shift = seg2 - search_range
    end = [cp for cp, _ in items]
    start = [cp for cp, _ in items]
    delta_signed = [((gid - cp) & 0xFFFF) if ((gid - cp) & 0xFFFF) < 0x8000
                    else ((gid - cp) & 0xFFFF) - 0x10000 for cp, gid in items]
    zero = [0] * seg_count
    length = 14 + seg2 + 2 + seg2 + seg2 + seg2
    buf = bytearray(struct.pack(">HHHHHHH", 4, length, 0, seg2, search_range,
                                entry_selector, range_shift))
    buf += struct.pack(f">{seg_count}H", *end)
    buf += struct.pack(">H", 0)  # reservedPad
    buf += struct.pack(f">{seg_count}H", *start)
    buf += struct.pack(f">{seg_count}h", *delta_signed)
    buf += struct.pack(f">{seg_count}H", *zero)
    return bytes(buf)


def _build_cmap_format12(mapping: dict[int, int]) -> bytes:
    items = sorted(mapping.items())
    n = len(items)
    length = 16 + n * 12
    buf = bytearray(struct.pack(">HHIII", 12, 0, length, 0, n))
    for cp, gid in items:
        buf += struct.pack(">III", cp, cp, gid)
    return bytes(buf)


class PdfRenderer:
    def __init__(self, font_path=None):
        self.font_path = font_path or discover_font()
        if self.font_path is None:
            raise ProtocolError("PDF 渲染需要系统中文字体（.ttf/.ttc）；未找到可用字体")
        self.font = load_font(self.font_path)

    # -- 排版 ---------------------------------------------------------------
    def _codes_for(self, text: str) -> list[tuple[int, int]]:
        """返回 [(unicode, glyph_id)]，Unicode 用于 ToUnicode，glyph_id 作为字符码。"""
        out = []
        for ch in text:
            cp = ord(ch)
            gid = self.font.glyph_map.get(cp, 0)
            out.append((cp, gid))
        return out

    def _char_w(self, gid: int, size: float) -> float:
        return self.font.width_units(gid) / self.font.units_per_em * size

    def _wrap(self, text: str, size: float, max_w: float) -> list[list[int]]:
        """按像素贪心换行，返回每行 [(cp, gid)]。优先在空格处断行。"""
        chars = self._codes_for(text)
        lines, cur, cur_w = [], [], 0.0
        last_space = -1
        for cp, gid in chars:
            wid = self._char_w(gid, size)
            if cur_w + wid <= max_w or not cur:
                if cp == 0x20:
                    last_space = len(cur)
                cur.append((cp, gid))
                cur_w += wid
                continue
            # 溢出：优先回到上一个空格
            if last_space > 0:
                lines.append(cur[:last_space])
                cur = cur[last_space + 1:] + [(cp, gid)]
                cur_w = sum(self._char_w(g, size) for _, g in cur)
                last_space = -1
            else:
                lines.append(cur)
                cur, cur_w = [(cp, gid)], wid
                last_space = -1
        if cur:
            lines.append(cur)
        return lines

    def _compose(self, blocks) -> list[list[dict]]:
        """blocks: [(kind, text)]。返回 per-ppage 的 [(size, glyphs, x, baseline)]。"""
        max_w = PAGE_W - 2 * MARGIN
        pages, current_y = [], PAGE_H - MARGIN
        current = []
        for kind, text in blocks:
            size = SIZES.get(kind, 11)
            for line in self._wrap(text, size, max_w):
                if current_y - size * LINE_GAP < MARGIN:
                    pages.append(current)
                    current, current_y = [], PAGE_H - MARGIN
                current.append((size, line, current_y))
                current_y -= size * LINE_GAP
        if current:
            pages.append(current)
        return pages

    # -- ToUnicode ----------------------------------------------------------
    def _to_unicode(self, code_to_unicode: dict[int, int], num_entries: int) -> bytes:
        items = sorted(code_to_unicode.items())
        cmap = (
            b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            b"/CIDSystemInfo<< /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            b"/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
            b"1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n")
        cmap += f"{num_entries} beginbfchar\n".encode("ascii")
        for code, cp in items:
            if cp <= 0xFFFF:
                dst = f"{cp:04X}"
            else:  # 增补平面 → UTF-16BE 代理对
                v = cp - 0x10000
                dst = f"{(0xD800 + (v >> 10)):04X}{(0xDC00 + (v & 0x3FF)):04X}"
            cmap += f"<{code:04X}> <{dst}>\n".encode("ascii")
        cmap += b"endbfchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
        return cmap

    # -- 字体子集化 ----------------------------------------------------------
    def _subset_font(self, remap: dict[int, int], unicode_to_new: dict[int, int]) -> bytes:
        """把原始 TrueType 裁剪为只含用到的字形、glyph 重排为连续编号子集字体。

        Identity-H 下字符码即内嵌字体 glyph 下标，故重排后才能让 PDF 渲染器正确取字形。
        """
        rev = {v: k for k, v in remap.items()}
        new_num = len(remap)
        font = self.font

        # glyf / loca 子集（始终使用长格式 loca）
        glyf = font._slice("glyf")
        loca = font._slice("loca")
        ocount = font.num_glyphs + 1
        if font.index_to_loc == 1:
            orig_off = struct.unpack_from(f">{ocount}I", loca, 0)
        else:
            raw = struct.unpack_from(f">{ocount}H", loca, 0)
            orig_off = [x * 2 for x in raw]
        new_glyf = bytearray()
        new_loca = [0] * (new_num + 1)
        for code in range(new_num):
            og = rev[code]
            new_loca[code] = len(new_glyf)
            o0, o1 = orig_off[og], orig_off[og + 1]
            if o1 > o0:
                new_glyf += glyf[o0:o1]
        new_loca[new_num] = len(new_glyf)
        ui32_loca = struct.pack(f">{new_num + 1}I", *new_loca)
        ui32_loca += b"\x00" * (-len(ui32_loca) % 4)

        # hmtx：每个子集字形保留原 advance，lsb 置 0，numberOfHMetrics 设为子集数
        new_hmtx = b"".join(struct.pack(">Hh", font.widths.get(rev[c], font.default_width), 0)
                            for c in range(new_num))
        new_cmap = self._build_cmap(unicode_to_new)
        # glyf 补 4 字节对齐
        new_glyf += b"\x00" * (-len(new_glyf) % 4)

        head = bytearray(font._slice("head"))
        struct.pack_into(">H", head, 50, 1)  # indexToLocFormat = 长格式
        struct.pack_into(">I", head, 8, 0)   # checkSumAdjustment 置 0
        maxp = bytearray(font._slice("maxp"))
        struct.pack_into(">H", maxp, 4, new_num)
        hhea = bytearray(font._slice("hhea"))
        struct.pack_into(">H", hhea, 34, new_num)  # numberOfHMetrics

        tables = {
            "cmap": new_cmap, "glyf": bytes(new_glyf), "head": bytes(head),
            "hhea": bytes(hhea), "hmtx": new_hmtx, "loca": ui32_loca,
            "maxp": bytes(maxp),
        }
        for tag in ("name", "OS/2"):
            if tag in font.tables:
                tables[tag] = font._slice(tag)
        return self._assemble_ttf(tables)

    @staticmethod
    def _assemble_ttf(tables: dict[str, bytes]) -> bytes:
        def checksum(data: bytes) -> int:
            d = data if len(data) % 4 == 0 else data + b"\x00" * (4 - len(data) % 4)
            total = 0
            for i in range(0, len(d), 4):
                total = (total + int.from_bytes(d[i:i + 4], "big")) & 0xFFFFFFFF
            return total

        tags = sorted(tables)
        num = len(tags)
        entry = 0
        while (1 << (entry + 1)) <= num:
            entry += 1
        search_range = (1 << entry) * 16
        range_shift = num * 16 - search_range
        out = bytearray(struct.pack(">IHHHH", 0x00010000, num, search_range, entry, range_shift))
        data = bytearray()
        offset = 12 + num * 16
        records = []
        for tag in tags:
            if offset % 4:
                pad = 4 - offset % 4
                data += b"\x00" * pad
                offset += pad
            rec_off = offset
            body = tables[tag]
            data += body
            offset += len(body)
            records.append((tag.encode("latin1"), checksum(body), rec_off, len(body)))
        for tag, cksum, rec_off, length in records:
            out += tag + struct.pack(">III", cksum, rec_off, length)
        out += data
        return bytes(out)

    @staticmethod
    def _build_cmap(unicode_to_new: dict[int, int]) -> bytes:
        """从 unicode→子集码构建最小合法 cmap（format 4 覆盖 BMP，format 12 覆盖增补平面）。"""
        bmp = {cp: g for cp, g in unicode_to_new.items() if cp <= 0xFFFF}
        supp = {cp: g for cp, g in unicode_to_new.items() if cp > 0xFFFF}
        encs: list[tuple[int, int, bytes]] = []
        if bmp:
            encs.append((3, 1, _build_cmap_format4(bmp)))
        if supp:
            encs.append((3, 10, _build_cmap_format12(supp)))
        if not encs:
            encs.append((3, 1, _build_cmap_format4({0x20: 0})))
        num = len(encs)
        header_len = 4 + num * 8
        offsets, running = [], header_len
        for _, _, sub in encs:
            offsets.append(running)
            running += len(sub) + (-len(sub) % 4)
        out = bytearray(struct.pack(">HH", 0, num))
        for j, (pid, eid, _) in enumerate(encs):
            out += struct.pack(">HHI", pid, eid, offsets[j])
        while len(out) < offsets[0]:
            out += b"\x00"
        for j, (_, _, sub) in enumerate(encs):
            out += sub
            if j < num - 1:
                out += b"\x00" * (-len(sub) % 4)
        return bytes(out)

    # -- 组装 ---------------------------------------------------------------
    def render(self, blocks, out_path: str | Path) -> str:
        pages = self._compose(blocks)
        if not pages:
            pages = [[(11, [(0x20, self.font.glyph_map.get(0x20, 0))], PAGE_H - MARGIN)]]

        # 收集用到的原始 glyph 与 unicode↔glyph 映射（原 glyph_id 过大，需重排为连续子集号）
        used_orig: list[int] = []
        seen: set[int] = set()
        uni_to_gid: dict[int, int] = {}
        for page in pages:
            for _, glyphs, _ in page:
                for cp, gid in glyphs:
                    if gid not in seen:
                        seen.add(gid)
                        used_orig.append(gid)
                    uni_to_gid[cp] = gid
        # 子集重排：新码从 0(.notdef) 开始递增，保证 Identity-H 下 字符码 == 内嵌字体 glyph 下标
        remap: dict[int, int] = {0: 0}
        for g in used_orig:
            if g not in remap:
                remap[g] = len(remap)
        unicode_to_new = {cp: remap[g] for cp, g in uni_to_gid.items()}
        font_file = self._subset_font(remap, unicode_to_new)

        # 用子集码建立宽度/ToUnicode 映射
        code_widths: dict[int, int] = {}
        code_to_unicode: dict[int, int] = {}
        for page in pages:
            for _, glyphs, _ in page:
                for cp, gid in glyphs:
                    sc = remap[gid]
                    code_widths[sc] = self.font.width_units(gid)
                    code_to_unicode[sc] = cp

        # /W：按 glyph 升序，把连续同宽区间合并 [cFirst cLast [w]]
        w_parts: list[str] = []
        sorted_codes = sorted(code_widths)
        if sorted_codes:
            run_start = run_w = sorted_codes[0]
            wait_prev = code_widths[run_start]
            run_end = run_start
            for code in sorted_codes[1:]:
                w = code_widths[code]
                if code == run_end + 1 and w == wait_prev:
                    run_end = code
                else:
                    if run_start == run_end:
                        w_parts.append(f"{run_start} [{wait_prev}]")
                    else:
                        w_parts.append(f"{run_start} {run_end} [{wait_prev}]")
                    run_start = run_end = code
                    wait_prev = w
            if run_start == run_end:
                w_parts.append(f"{run_start} [{wait_prev}]")
            else:
                w_parts.append(f"{run_start} {run_end} [{wait_prev}]")
        w_array = " ".join(w_parts) or "0 [1000]"
        dw = round(self.font.default_width / self.font.units_per_em * 1000) or 1000

        add_ = self.font.ps_name[:32].split("+")[-1]
        base_font = f"AAAAAA+{add_}"

        # 对象 1..8
        catalog = b"<< /Type /Catalog /Pages 2 0 R >>"
        pages_dict = b"<< /Type /Pages /Kids [%s] /Count %d >>"
        resources = b"<< /Font << /F1 4 0 R >> >>"
        font_obj = (
            b"<< /Type /Font /Subtype /Type0 /BaseFont /" + base_font.encode("latin1") +
            b" /Encoding /Identity-H /DescendantFonts [5 0 R] /ToUnicode 7 0 R >>"
        )
        desc_font = (
            b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /" + base_font.encode("latin1") +
            b" /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >>"
            b" /FontDescriptor 6 0 R /DW " + str(dw).encode("ascii") +
            b" /W [" + w_array.encode("ascii") + b"] >>"
        )
        font_descriptor = (
            b"<< /Type /FontDescriptor /FontName /" + base_font.encode("latin1") +
            b" /Flags 4 /FontBBox [-0 -200 " + str(round(PAGE_W)).encode("ascii") +
            b" " + str(round(self.font.units_per_em)).encode("ascii") +
            b"] /ItalicAngle 0 /Ascent 1000 /Descent -200"
            b" /CapHeight " + str(self.font.units_per_em).encode("ascii") +
            b" /StemV 80 /FontFile2 8 0 R >>"
        )
        to_unicode = self._to_unicode(code_to_unicode, len(code_to_unicode))

        # 计算页数以分配对象号
        p = len(pages)
        n_content = p
        # 对象号分配：
        # 1 catalog, 2 pages, 3 resources, 4 font, 5 desc, 6 fd, 7 tounicode, 8 fontfile,
        # 9..9+p-1 content, 之后 p 个 page 对象
        content_start = 9
        content_nums = list(range(content_start, content_start + n_content))
        page_nums = list(range(content_start + n_content, content_start + 2 * n_content))
        total = page_nums[-1] if page_nums else 8

        kids = " ".join(f"{n} 0 R" for n in page_nums)
        pages_body = (pages_dict % (kids.encode("ascii"), p))

        parts = {1: catalog, 2: pages_body, 3: resources, 4: font_obj,
                 5: desc_font, 6: font_descriptor, 7: None, 8: None}
        parts[7] = (b"<< /Length " + str(len(to_unicode)).encode("ascii") + b" >>\nstream\n"
                    + to_unicode + b"\nendstream")
        parts[8] = (b"<< /Length " + str(len(font_file)).encode("ascii")
                    + b" /Length1 " + str(len(font_file)).encode("ascii")
                    + b" >>\nstream\n" + font_file + b"\nendstream")

        # 正文流逐页构建
        for idx, page_lines in enumerate(pages):
            content = bytearray()
            for size, glyphs, baseline in page_lines:
                hexdata = "".join(f"{remap[gid]:04X}" for _, gid in glyphs).encode("ascii")
                content += (
                    f"BT /F1 {size:g} Tf 1 0 0 1 {MARGIN:.2f} {baseline:.2f} Tm "
                    .encode("ascii") + b"<" + hexdata + b"> Tj ET\n"
                )
            parts[content_nums[idx]] = (b"<< /Length " + str(len(content)).encode("ascii")
                                        + b" >>\nstream\n" + bytes(content) + b"\nendstream")
        # 页面对象
        for idx, page_num in enumerate(page_nums):
            parts[page_num] = (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 " +
                f"{PAGE_W:.2f} {PAGE_H:.2f}".encode("ascii") +
                b"] /Resources 3 0 R /Contents " + str(content_nums[idx]).encode("ascii") +
                b" 0 R >>"
            )

        # 写出
        output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0] * (total + 1)
        for num in range(1, total + 1):
            offsets[num] = len(output)
            output += f"{num} 0 obj\n".encode("ascii")
            output += parts[num]
            output += b"\nendobj\n"
        xref = len(output)
        output += f"xref\n0 {total + 1}\n".encode("ascii")
        output += b"0000000000 65535 f \n"
        for num in range(1, total + 1):
            output += f"{offsets[num]:010d} 00000 n \n".encode("ascii")
        output += (f"trailer\n<< /Size {total + 1} /Root 1 0 R >>\n"
                   f"startxref\n{xref}\n%%EOF\n".encode("ascii"))

        out_path = Path(out_path)
        out_path.write_bytes(bytes(output))
        return str(out_path)