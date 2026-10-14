#!/usr/bin/env python3
"""把 Markdown 转成 Word 文档（.docx）。

！只用 Python 标准库，不依赖 pandoc / python-docx / 已安装的 Word。
  .docx 本质就是一个 zip，里面装着几份 XML。这里直接把 OOXML 拼出来。

支持本项目文档用到的语法：标题(1-6)、段落、无序/有序列表、表格、引用、
围栏代码块、分隔线、行内 **粗体** / *斜体* / `代码` / [链接](地址)。

用法：
    python md2docx.py                          # 总结.md -> 总结.docx
    python md2docx.py 别的.md                  # 别的.md -> 别的.docx
    python md2docx.py 输入.md 输出.docx

生成的文档里，标题是真正的「标题 1/2/3」样式，所以在 Word 里可以直接用
导航窗格、自动生成目录、统一改样式表。
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

BASE = Path(__file__).resolve().parent

# =============================================================================
# XML 基础
# =============================================================================

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

A4_W, A4_H = 11906, 16838                 # twips，A4 尺寸
SZ_BODY, SZ_CODE = 21, 19                 # 半磅，21 = 10.5pt


def esc(text: str) -> str:
    """XML 文本转义。必须先做这一步，再拼任何标签。"""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


def attr(text: str) -> str:
    return esc(text).replace('"', "&quot;")


# =============================================================================
# 段落 / 文字
# =============================================================================

def run(text: str, *, bold=False, italic=False, code=False) -> str:
    """一个 <w:r>。rPr 里的子元素要按 OOXML schema 的顺序排，否则 Word 可能不认。"""
    rpr: list[str] = []
    if code:
        rpr.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" '
                   'w:eastAsia="Consolas" w:cs="Consolas"/>')
    if bold:
        rpr.append("<w:b/><w:bCs/>")
    if italic:
        rpr.append("<w:i/><w:iCs/>")
    if code:
        rpr.append('<w:color w:val="A33A18"/>')
        rpr.append(f'<w:sz w:val="{SZ_CODE}"/><w:szCs w:val="{SZ_CODE}"/>')
        rpr.append('<w:shd w:val="clear" w:color="auto" w:fill="F2F4F2"/>')
    else:
        rpr.append(f'<w:sz w:val="{SZ_BODY}"/><w:szCs w:val="{SZ_BODY}"/>')
    return (f"<w:r><w:rPr>{''.join(rpr)}</w:rPr>"
            f'<w:t xml:space="preserve">{esc(text)}</w:t></w:r>')


def para(runs_xml: str, *, style: str | None = None, ind: int | None = None,
         shading: str | None = None) -> str:
    """一个 <w:p>。pPr 子元素顺序：pStyle → pBdr → shd → spacing → ind。"""
    ppr: list[str] = []
    if style:
        ppr.append(f'<w:pStyle w:val="{style}"/>')
    if shading:
        ppr.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{shading}"/>')
    if ind is not None:
        ppr.append(f'<w:ind w:left="{ind}"/>')
    if not ppr:
        return f"<w:p>{runs_xml}</w:p>"
    return f"<w:p><w:pPr>{''.join(ppr)}</w:pPr>{runs_xml}</w:p>"


# =============================================================================
# 行内解析
# =============================================================================

# 先摘 `代码`，再摘 **粗体**，最后才轮到 *斜体*
_INLINE_RE = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*|\*[^*\n]+\*)")


def inline_runs(text: str) -> str:
    """把一行文字转成一串 <w:r>。"""
    # [文字](地址) —— 文档里的链接都是 (#锚点)，在 Word 里跳不过去，
    # 保留文字、丢掉链接（点不动的假链接比纯文字更误导人）
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    # <https://...> 自动链接 -> 纯文字
    text = re.sub(r"<((?:https?)://[^>\s]+)>", r"\1", text)

    out: list[str] = []
    for piece in _INLINE_RE.split(text):
        if not piece:
            continue
        if piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            out.append(run(piece[1:-1], code=True))
        elif piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            out.append(run(piece[2:-2], bold=True))
        elif piece.startswith("*") and piece.endswith("*") and len(piece) > 2:
            out.append(run(piece[1:-1], italic=True))
        else:
            out.append(run(piece))
    return "".join(out) or run("")


# =============================================================================
# 表格
# =============================================================================

def split_row(line: str) -> list[str]:
    """拆一行。被 \\| 转义过的竖线不算分隔符。"""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.replace("\\|", "|").strip() for cell in body.split("|")]


def is_separator(line: str) -> bool:
    return bool(re.fullmatch(r"\|[\s:|-]+\|?\s*", line.strip()))


def table_xml(header: list[str], rows: list[list[str]]) -> str:
    columns = max(len(header), 1)
    col_w = (A4_W - 1021 * 2) // columns          # 减去左右页边距

    def cell(text: str, *, head: bool) -> str:
        shd = ('<w:shd w:val="clear" w:color="auto" w:fill="E8F0E6"/>'
               if head else "")
        return (
            "<w:tc><w:tcPr>"
            f'<w:tcW w:w="{col_w}" w:type="dxa"/>{shd}'
            '<w:vAlign w:val="top"/>'
            "</w:tcPr>"
            + para(run(text, bold=True) if head else inline_runs(text))
            + "</w:tc>"
        )

    grid = "".join(f'<w:gridCol w:w="{col_w}"/>' for _ in range(columns))
    pieces = ['<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/>',
              '<w:tblW w:w="5000" w:type="pct"/><w:tblBorders>']
    for edge, color in (("top", "9DB29A"), ("left", "CFDACD"),
                        ("bottom", "9DB29A"), ("right", "CFDACD"),
                        ("insideH", "CFDACD"), ("insideV", "CFDACD")):
        pieces.append(f'<w:{edge} w:val="single" w:sz="4" w:space="0" '
                      f'w:color="{color}"/>')
    pieces.append("</w:tblBorders></w:tblPr>")
    pieces.append(f"<w:tblGrid>{grid}</w:tblGrid>")
    pieces.append("<w:tr>" + "".join(cell(c, head=True) for c in header) + "</w:tr>")
    for row in rows:
        padded = (list(row) + [""] * columns)[:columns]
        pieces.append("<w:tr>"
                      + "".join(cell(c, head=False) for c in padded)
                      + "</w:tr>")
    pieces.append("</w:tbl>")
    # ！表格后面要跟一个空段落，否则相邻的两张表会被并到一起
    pieces.append("<w:p/>")
    return "".join(pieces)


# =============================================================================
# Markdown -> 文档体
# =============================================================================

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
UL_RE = re.compile(r"^[-*]\s+(.*)$")
OL_RE = re.compile(r"^(\d+)\.\s+(.*)$")
HR_RE = re.compile(r"^-{3,}$|^\*{3,}$|^_{3,}$")
FENCE_RE = re.compile(r"^```\s*(\S*)\s*$")


def build_body(markdown: str) -> str:
    lines = markdown.splitlines()
    out: list[str] = []
    i, total = 0, len(lines)

    while i < total:
        line = lines[i]

        # --- 围栏代码块 ---------------------------------------------------
        fence = FENCE_RE.match(line.strip())
        if fence:
            i += 1
            block: list[str] = []
            while i < total and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1                                   # 吃掉结尾那行 ```
            if fence.group(1) == "mermaid":
                out.append(para(
                    run("（下面是 Mermaid 流程图源码。Word 不认 mermaid，"
                        "要看渲染出来的图请用编辑器或浏览器打开 .md 文件）"),
                    style="Meta"))
            for code_line in block or [""]:
                out.append(para(run(code_line, code=True), style="CodeBlock"))
            continue

        # --- 表格 ---------------------------------------------------------
        if (line.strip().startswith("|") and i + 1 < total
                and is_separator(lines[i + 1])):
            header = split_row(line)
            i += 2
            rows: list[list[str]] = []
            while i < total and lines[i].strip().startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            out.append(table_xml(header, rows))
            continue

        # --- 标题 ---------------------------------------------------------
        heading = HEADING_RE.match(line)
        if heading:
            level = min(len(heading.group(1)), 4)
            out.append(para(inline_runs(heading.group(2)),
                            style=f"Heading{level}"))
            i += 1
            continue

        # --- 分隔线 -------------------------------------------------------
        if HR_RE.match(line.strip()):
            out.append(para("", style="HrLine"))
            i += 1
            continue

        # --- 引用 ---------------------------------------------------------
        if line.lstrip().startswith(">"):
            while i < total and lines[i].lstrip().startswith(">"):
                item = re.sub(r"^\s*>\s?", "", lines[i])
                if item.strip():
                    out.append(para(inline_runs(item), style="Quote"))
                i += 1
            continue

        # --- 列表 ---------------------------------------------------------
        # ！用文字符号（• / 1.）而不是 OOXML 的 numbering.xml：
        #   那需要额外一份 numbering.xml 并维护 numId 映射，收益只是「能在
        #   Word 里自动重排编号」，对这个文档不值这个复杂度。
        ordered = OL_RE.match(line)
        bulleted = UL_RE.match(line)
        if ordered or bulleted:
            pattern = OL_RE if ordered else UL_RE
            while i < total:
                match = pattern.match(lines[i])
                if not match:
                    break
                if ordered:
                    marker, text = match.group(1) + "  ", match.group(2)
                else:
                    marker, text = "•  ", match.group(1)
                out.append(para(run(marker) + inline_runs(text),
                                style="ListItem"))
                i += 1
            continue

        # --- 空行 ---------------------------------------------------------
        if not line.strip():
            i += 1
            continue

        # --- 普通段落 -----------------------------------------------------
        out.append(para(inline_runs(line)))
        i += 1

    return "".join(out)


# =============================================================================
# 固定的几份 XML
# =============================================================================

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

ROOT_RELS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="{R_NS}/officeDocument" Target="word/document.xml"/>
</Relationships>"""

DOC_RELS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="{R_NS}/styles" Target="styles.xml"/>
</Relationships>"""

BORDERS_XML = ("<w:tblBorders>"
               + "".join(f'<w:{edge} w:val="single" w:sz="4" w:space="0" '
                         f'w:color="{color}"/>'
                         for edge, color in (("top", "9DB29A"),
                                             ("left", "CFDACD"),
                                             ("bottom", "9DB29A"),
                                             ("right", "CFDACD"),
                                             ("insideH", "CFDACD"),
                                             ("insideV", "CFDACD")))
               + "</w:tblBorders>")


def style(style_id: str, name: str, *, para_props: str = "",
          run_props: str = "", based: str = "Normal") -> str:
    inherit = f'<w:basedOn w:val="{based}"/>' if based else ""
    return (f'<w:style w:type="paragraph" w:styleId="{style_id}">'
            f'<w:name w:val="{attr(name)}"/>{inherit}'
            f"<w:pPr>{para_props}</w:pPr><w:rPr>{run_props}</w:rPr>"
            "</w:style>")


STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W_NS}">
<w:docDefaults>
<w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Microsoft YaHei" w:cs="Calibri"/>
<w:sz w:val="{SZ_BODY}"/><w:szCs w:val="{SZ_BODY}"/>
</w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr>
<w:spacing w:after="80" w:line="288" w:lineRule="auto"/>
</w:pPr></w:pPrDefault>
</w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal">
<w:name w:val="Normal"/><w:qFormat/>
</w:style>
{style("Heading1", "heading 1",
       para_props=('<w:keepNext/><w:outlineLvl w:val="0"/>'
                   '<w:spacing w:before="320" w:after="140"/>'
                   '<w:pBdr><w:bottom w:val="single" w:sz="18" w:space="3" '
                   'w:color="2E7D32"/></w:pBdr>'),
       run_props=('<w:b/><w:bCs/><w:color w:val="14401A"/>'
                  '<w:sz w:val="40"/><w:szCs w:val="40"/>'))}
{style("Heading2", "heading 2",
       para_props=('<w:keepNext/><w:outlineLvl w:val="1"/>'
                   '<w:spacing w:before="280" w:after="110"/>'
                   '<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="2" '
                   'w:color="C9D6C9"/></w:pBdr>'),
       run_props=('<w:b/><w:bCs/><w:color w:val="14401A"/>'
                  '<w:sz w:val="30"/><w:szCs w:val="30"/>'))}
{style("Heading3", "heading 3",
       para_props='<w:keepNext/><w:outlineLvl w:val="2"/>'
                  '<w:spacing w:before="220" w:after="90"/>',
       run_props=('<w:b/><w:bCs/><w:color w:val="2E5D33"/>'
                  '<w:sz w:val="24"/><w:szCs w:val="24"/>'))}
{style("Heading4", "heading 4",
       para_props='<w:keepNext/><w:outlineLvl w:val="3"/>'
                  '<w:spacing w:before="180" w:after="80"/>',
       run_props=('<w:b/><w:bCs/><w:color w:val="2E5D33"/>'
                  '<w:sz w:val="22"/><w:szCs w:val="22"/>'))}
{style("CodeBlock", "Code Block",
       para_props=('<w:shd w:val="clear" w:color="auto" w:fill="F6F8F6"/>'
                   '<w:spacing w:before="0" w:after="0" w:line="240" '
                   'w:lineRule="auto"/><w:ind w:left="120"/><w:keepLines/>'),
       run_props=('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" '
                  'w:eastAsia="Consolas" w:cs="Consolas"/>'
                  '<w:sz w:val="18"/><w:szCs w:val="18"/>'))}
{style("Quote", "Quote",
       para_props=('<w:shd w:val="clear" w:color="auto" w:fill="F4F7F4"/>'
                   '<w:spacing w:before="60" w:after="60"/>'
                   '<w:ind w:left="240"/>'
                   '<w:pBdr><w:left w:val="single" w:sz="18" w:space="6" '
                   'w:color="7CB342"/></w:pBdr>'),
       run_props='<w:color w:val="333333"/>')}
{style("ListItem", "List Item",
       para_props='<w:spacing w:after="20"/>'
                  '<w:ind w:left="360" w:hanging="200"/>')}
{style("Meta", "Meta Note",
       para_props='<w:spacing w:before="80" w:after="40"/>',
       run_props=('<w:color w:val="6B6B6B"/><w:i/>'
                  '<w:sz w:val="19"/><w:szCs w:val="19"/>'))}
{style("HrLine", "HR Line",
       para_props=('<w:spacing w:before="120" w:after="120"/>'
                   '<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" '
                   'w:color="C9D6C9"/></w:pBdr>'), based="")}
<w:style w:type="table" w:styleId="TableGrid">
<w:name w:val="Table Grid"/>
<w:tblPr>{BORDERS_XML}<w:tblCellMar>
<w:top w:w="40" w:type="dxa"/><w:left w:w="80" w:type="dxa"/>
<w:bottom w:w="40" w:type="dxa"/><w:right w:w="80" w:type="dxa"/>
</w:tblCellMar></w:tblPr>
</w:style>
</w:styles>"""

SECT_PR = (f'<w:sectPr><w:pgSz w:w="{A4_W}" w:h="{A4_H}"/>'
           '<w:pgMar w:top="1134" w:right="1021" w:bottom="1134" '
           'w:left="1021" w:header="851" w:footer="992" w:gutter="0"/>'
           "</w:sectPr>")

DOCUMENT_HEAD = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 f'<w:document xmlns:w="{W_NS}" xmlns:r="{R_NS}"><w:body>')
DOCUMENT_TAIL = "</w:body></w:document>"


# =============================================================================
# 打包与自检
# =============================================================================

def build_docx(markdown: str, dst: Path) -> None:
    parts = {
        "[Content_Types].xml": CONTENT_TYPES,
        "_rels/.rels": ROOT_RELS,
        "word/_rels/document.xml.rels": DOC_RELS,
        "word/styles.xml": STYLES,
        "word/document.xml": (DOCUMENT_HEAD + build_body(markdown)
                              + SECT_PR + DOCUMENT_TAIL),
    }

    # 先自己验一遍 XML 是不是良构的 —— 让 Word 报「文件已损坏」再来查太费劲
    for name, text in parts.items():
        try:
            ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            raise SystemExit(f"[md2docx] 内部错误：{name} 不是合法 XML：{exc}")

    if dst.exists():
        dst.unlink()
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        # ！[Content_Types].xml 必须是第一个条目，有解析器依赖这个顺序
        zf.writestr("[Content_Types].xml", parts.pop("[Content_Types].xml"))
        for name, text in parts.items():
            zf.writestr(name, text)


def verify(dst: Path) -> str:
    """读回来确认它确实是个能打开的 docx，并报一下里面有多少东西。"""
    with zipfile.ZipFile(dst) as zf:
        bad = zf.testzip()
        if bad:
            raise SystemExit(f"[md2docx] 生成的压缩包里 {bad} 已损坏")
        names = [n for n in zf.namelist()
                 if n.endswith(".xml") or n.endswith(".rels")]
        for name in names:
            ElementTree.fromstring(zf.read(name))
        document = zf.read("word/document.xml").decode("utf-8")
    return (f"{dst.stat().st_size / 1024:.0f} KB，{len(names)} 份 XML 全部良构；"
            f"段落 {document.count('<w:p>') + document.count('<w:p/>')} 个，"
            f"表格 {document.count('<w:tbl>')} 张，"
            f"标题 {len(re.findall('Heading[1-4]', document))} 个")


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "总结.md"
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".docx")

    if not src.exists():
        print(f"[md2docx] 找不到源文件：{src}")
        return 1

    build_docx(src.read_text(encoding="utf-8"), dst)
    print(f"[md2docx] 完成：{dst}")
    print(f"[md2docx] 自检：{verify(dst)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
