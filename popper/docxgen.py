"""DOCX（OOXML）稿件渲染器。仅用标准库（zipfile + 字符串拼接）生成合法 Word 文档。

直接构造 WordprocessingML 包结构（[Content_Types].xml / _rels / document.xml / styles.xml），
段落使用命名样式（Title / Heading1 / Heading2 / Note）形成清晰层级，中文无需额外字体即正确显示。
"""
from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from .core import ProtocolError

# 段落样式 → w:pStyle
KIND_STYLE = {"title": "Title", "label": "Heading1", "section": "Heading2",
              "body": "Normal", "note": "Note"}


def _par(style: str, text: str) -> str:
    return (f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
            f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>')


CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '</Types>'
)

RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    '</Relationships>'
)

DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
    'Target="styles.xml"/>'
    '</Relationships>'
)

STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:docDefaults><w:rPrDefault><w:rPr><w:sz w:val="22"/></w:rPr></w:rPrDefault>'
    '<w:pPrDefault><w:pPr/></w:pPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    '<w:name w:val="Normal"/><w:qFormat/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Title">'
    '<w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:pPr><w:jc w:val="center"/>'
    '<w:spacing w:after="240"/></w:pPr><w:rPr><w:b/><w:sz w:val="36"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1">'
    '<w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>'
    '<w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr>'
    '<w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2">'
    '<w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>'
    '<w:pPr><w:spacing w:before="200" w:after="80"/></w:pPr>'
    '<w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Note">'
    '<w:name w:val="Note"/><w:basedOn w:val="Normal"/>'
    '<w:rPr><w:i/><w:color w:val="808080"/></w:rPr></w:style>'
    '</w:styles>'
)


class DocxRenderer:
    def render(self, blocks, out_path: str | Path) -> str:
        if not blocks:
            raise ProtocolError("无可渲染的稿件内容")
        paragraphs = []
        for kind, text in blocks:
            style = KIND_STYLE.get(kind, "Normal")
            # 多段正文：按空行拆分为多个段落，保持段落间距
            for para in str(text).split("\n"):
                paragraphs.append(_par(style, para))
        body = ("<w:body>"
                + "".join(paragraphs)
                + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                  '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
                + "</w:body>")
        document = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<w:document '
            'xmlns:wpc="http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas" '
            'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            + body + "</w:document>"
        )
        out_path = Path(out_path)
        with ZipFile(out_path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr("_rels/.rels", RELS)
            archive.writestr("word/_rels/document.xml.rels", DOC_RELS)
            archive.writestr("word/styles.xml", STYLES)
            archive.writestr("word/document.xml", document)
        return str(out_path)