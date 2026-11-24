"""MATLAB and Simulink extraction without MATLAB (A05, CP17.2; decision 0040).

Pure functions over bytes; nothing here opens a path, runs MATLAB or writes anything. The same
functions feed PrometheOS's adoption analyzers and, through :func:`extract_matlab` and
:func:`extract_simulink`, return Graphify-shaped ``{"nodes": [...], "edges": [...]}`` so the
module can later be offered as a Graphify extractor (upstream contribution needs the owner).

- ``.m``: Objective-C or MATLAB by GitHub linguist's rule order (Objective-C directives first,
  then MATLAB comment, ``function`` or ``classdef`` lines), else ``unknown``; MATLAB code is
  parsed with tree-sitter-matlab (pinned; the optional ``adoption-parsers`` extra, decision
  0053) for definitions, calls and I/O literals. Without the extra, :func:`parser_problem`
  says so, no code is parsed and a script without comment, function or classdef lines is
  ``unknown``; the other readers here need no parser.
- ``.mlx``: the code paragraphs of ``matlab/document.xml`` parsed as ``.m``.
- ``.slx`` (OPC ZIP) and ``.mdl`` (MathWorks OPC Text Package, or the classic ``Model {``
  text): package metadata (uuid, release, revision, created, modified), model references
  (``ModelReference`` blocks, ``ModelNameDialog``), library links (``SourceBlock``) and
  workspace variables (``FromWorkspace``/``ToWorkspace`` ``VariableName``).
- ``.slxc``: the model a Simulink cache belongs to.
- MAT-files: the v4/v5 header and variable directory, v7.3 recognised (HDF5, not read).
- MATLAB projects: ``.prj`` with ``resources/project`` metadata in the fixedPathV2 layout
  (file list and labels), and ``matlab.toml`` recognised.

Element names MathWorks does not document were confirmed on models saved by MATLAB R2024b
(decision 0040); the formats are "subject to change", so every reader tolerates what it does not
recognise and reports it instead of guessing.
"""

from __future__ import annotations

import io
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any
from xml.etree import ElementTree

# Budgets for reading packages and code (review cp17-234-a F21): a small file cannot expand
# into gigabytes of parts to parse, and an enormous script is not parsed.
MAX_PART_BYTES = 8 * 1024 * 1024
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_MAT_ELEMENTS = 10_000
MAX_DECOMPRESSED_HEADER = 64 * 1024

# ----- language of a .m file (linguist heuristics.yml order) ----------------------------------

_OBJC = re.compile(
    r"^\s*(@(interface|class|protocol|property|end|synchronised|selector|implementation)\b"
    r"|#import\s+.+\.h[\">])",
    re.MULTILINE,
)
_MERCURY = re.compile(r":- module", re.MULTILINE)
_MUF = re.compile(r"^: ", re.MULTILINE)
_MUMPS = re.compile(r"^\s*;", re.MULTILINE)
_WOLFRAM = re.compile(r"\(\*", re.MULTILINE)
_MATLAB = re.compile(
    r"^\s*%|^[ \t]*function[ \t]+(?:\[[^\]\r\n]*\][ \t]*=[ \t]*|[A-Za-z]\w*[ \t]*=[ \t]*)?"
    r"[A-Za-z]\w*[ \t]*\(|^[ \t]*classdef[ \t]+(?:\([^\r\n)]*\)[ \t]*)?[A-Za-z]\w*",
    re.MULTILINE,
)


def language_of(text: str) -> str:
    """``objective-c``, ``matlab`` or ``unknown`` (Mercury, MUF, MUMPS and Wolfram files are
    ``unknown`` here: adoption does not analyse them)."""
    if _OBJC.search(text):
        return "objective-c"
    if _MERCURY.search(text) or _MUF.search(text) or _MUMPS.search(text):
        return "unknown"
    if _WOLFRAM.search(text) and re.search(r"\*\)\s*$", text, re.MULTILINE):
        return "unknown"
    if _MATLAB.search(text):
        return "matlab"
    if len(text) <= MAX_BARE_SCRIPT and _bare_script(text):
        return "matlab"
    return "unknown"


MAX_BARE_SCRIPT = 1024 * 1024


def _bare_script(text: str) -> bool:
    """A script with no comment line, function or classdef (review cp17-234-a F17): MATLAB
    when, the other languages excluded, it parses without errors and assigns something. Plain
    words (``hello world``) parse as command syntax and stay unknown; without the parser
    nothing can be confirmed, so the script stays unknown too."""
    if parser_problem() is not None:
        return False
    root = _parser().parse(text.encode("utf-8", "replace")).root_node
    return not root.has_error and any(c.type == "assignment" for c in root.children)


# ----- MATLAB code structure -----------------------------------------------------------------

# Functions whose first literal argument names a file the code reads or writes.
READS = frozenset(
    {
        "load",
        "readtable",
        "readmatrix",
        "readcell",
        "readtimetable",
        "csvread",
        "dlmread",
        "xlsread",
        "importdata",
        "fileread",
        "imread",
        "audioread",
        "matfile",
        "jsondecode",
        "textscan",
        "fopen",
        "run",
        "open",
        "uiopen",
    }
)
WRITES = frozenset(
    {
        "save",
        "writetable",
        "writematrix",
        "writecell",
        "writetimetable",
        "csvwrite",
        "dlmwrite",
        "xlswrite",
        "imwrite",
        "audiowrite",
        "exportgraphics",
        "saveas",
        "print",
        "savefig",
    }
)
MODELS = frozenset({"sim", "open_system", "load_system", "new_system", "set_param", "get_param"})
# Writers whose file name is the second argument (the data or figure comes first).
DATA_SECOND = frozenset(
    {
        "writetable",
        "writematrix",
        "writecell",
        "writetimetable",
        "csvwrite",
        "dlmwrite",
        "xlswrite",
        "imwrite",
        "audiowrite",
        "exportgraphics",
        "saveas",
        "savefig",
    }
)
GRAPHIFY_VERSION_HINT = "graphify node/edge dict shape (0.9.x)"


@dataclass(frozen=True)
class Site:
    """A located fact in source text: 1-based line, byte offsets in the UTF-8 source."""

    name: str
    line: int
    start: int
    end: int
    text: str


@dataclass
class MatlabStructure:
    language: str
    parsed: bool
    errors: int
    definitions: list[Site] = field(default_factory=list)
    calls: list[Site] = field(default_factory=list)
    reads: list[Site] = field(default_factory=list)  # name = the literal path
    writes: list[Site] = field(default_factory=list)
    models: list[Site] = field(default_factory=list)
    assigned: set[str] = field(default_factory=set)


PARSER_EXTRA = "adoption-parsers"


@lru_cache(maxsize=1)
def parser_problem() -> str | None:
    """Why MATLAB code cannot be parsed here, or None when the pinned parser is installed."""
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_matlab  # noqa: F401
    except ImportError:
        return f"the MATLAB parser is not installed (the {PARSER_EXTRA} extra)"
    return None


@lru_cache(maxsize=1)
def _parser() -> Any:
    import tree_sitter_matlab
    from tree_sitter import Language, Parser

    return Parser(Language(tree_sitter_matlab.language()))


def _text(source: bytes, node: Any) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _site(source: bytes, node: Any, name: str) -> Site:
    line = node.start_point[0] + 1
    end_of_line = source.find(b"\n", node.start_byte)
    start_of_line = source.rfind(b"\n", 0, node.start_byte) + 1
    quote = source[start_of_line : end_of_line if end_of_line != -1 else len(source)]
    return Site(
        name, line, node.start_byte, node.end_byte, quote.decode("utf-8", "replace").strip()
    )


def _string_value(source: bytes, node: Any) -> str | None:
    if node.type != "string":
        return None
    raw = _text(source, node)
    if len(raw) >= 2 and raw[0] in "'\"" and raw[-1] == raw[0]:
        return raw[1:-1]
    return None


def _walk(node: Any) -> Any:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _assigned_names(source: bytes, node: Any) -> set[str]:
    """Variables a left-hand side, parameter list or loop iterator introduces: an identifier,
    the name of an indexed assignment ``m(k) = ...``, the first name of a field assignment, or
    every name of a ``[a, b] = ...`` list. Values on the right of an iterator are not names."""
    if node.type == "identifier":
        return {_text(source, node)}
    if node.type == "function_call":
        name = node.child_by_field_name("name")
        return {_text(source, name)} if name is not None and name.type == "identifier" else set()
    if node.type == "field_expression":
        first = next((c for c in node.children if c.type == "identifier"), None)
        return {_text(source, first)} if first is not None else set()
    if node.type == "iterator":
        first = next((c for c in node.children if c.type == "identifier"), None)
        return {_text(source, first)} if first is not None else set()
    names: set[str] = set()
    for child in node.children:
        if child.type in (
            "identifier",
            "multioutput_variable",
            "function_call",
            "field_expression",
        ):
            names |= _assigned_names(source, child)
    return names


def parse_matlab(source: bytes) -> MatlabStructure:
    """Definitions, calls, file literals and model names of MATLAB source (``.m`` text or the
    code of a live script). Calls include indexing (the grammar cannot tell ``a(1)`` from a
    call): callers resolve names against known functions and drop assigned variables."""
    text = source.decode("utf-8", "replace")
    language = language_of(text)
    result = MatlabStructure(language=language, parsed=False, errors=0)
    if language != "matlab" or len(source) > MAX_SOURCE_BYTES or parser_problem() is not None:
        return result  # not parsed: another language, over the source budget, or no parser
    tree = _parser().parse(source)
    result.parsed = True
    for node in _walk(tree.root_node):
        if node.type == "ERROR" or node.is_missing:
            result.errors += 1
        elif node.type == "function_definition":
            name = node.child_by_field_name("name")
            if name is not None:
                result.definitions.append(_site(source, name, _text(source, name)))
        elif node.type == "assignment":
            left = node.child_by_field_name("left") or (node.children[0] if node.children else None)
            if left is not None:
                result.assigned |= _assigned_names(source, left)
        elif node.type in ("function_output", "function_arguments", "iterator"):
            result.assigned |= _assigned_names(source, node)
        elif node.type == "function_call":
            name_node = node.child_by_field_name("name")
            if name_node is None or name_node.type != "identifier":
                continue
            name = _text(source, name_node)
            args = next((c for c in node.children if c.type == "arguments"), None)
            first = None
            if args is not None:
                named = [c for c in args.children if c.is_named]
                literal = named[0] if named else None
                first = _string_value(source, literal) if literal is not None else None
                if first is None and name in DATA_SECOND and len(named) > 1:
                    # writematrix(M, 'out.csv'), writetable(T, 'out.csv'), saveas(fig, 'f.png'):
                    # the file is the second argument (found by the CP17.5 re-run test)
                    first = _string_value(source, named[1])
            if name in READS and first:
                result.reads.append(_site(source, node, first))
            elif name in WRITES and first:
                result.writes.append(_site(source, node, first))
            elif name in MODELS and first:
                result.models.append(_site(source, node, first))
            if name not in READS and name not in WRITES and name not in MODELS:
                result.calls.append(_site(source, name_node, name))
        elif node.type == "command":
            name_node = next((c for c in node.children if c.type == "command_name"), None)
            if name_node is not None:
                name = _text(source, name_node)
                args = [c for c in node.children if c.type == "command_argument"]
                literal = _text(source, args[0]).strip("'\"") if args else None
                if name in READS and literal:
                    result.reads.append(_site(source, node, literal))
                elif name in WRITES and literal:
                    result.writes.append(_site(source, node, literal))
                elif name in MODELS and literal:
                    result.models.append(_site(source, node, literal))
    return result


def extract_matlab(path: str, source: bytes) -> dict[str, Any]:
    """Graphify-shaped extraction of one ``.m`` file: a file node, a node per function it
    defines (``contains``), and ``calls`` edges to the names it calls (EXTRACTED, located).
    Indexing that the grammar reports as a call is dropped when the name is assigned in the
    same file."""
    structure = parse_matlab(source)
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    file_id = _node_id(path)
    nodes = [
        {
            "id": file_id,
            "label": path.rsplit("/", 1)[-1],
            "file_type": "code",
            "source_file": path,
            "source_location": "L1",
        }
    ]
    edges: list[dict[str, Any]] = []
    if structure.language != "matlab":
        nodes[0]["language"] = structure.language
        return {"nodes": nodes, "edges": edges}
    defined = {d.name for d in structure.definitions}
    for d in structure.definitions:
        node_id = f"{file_id}_{d.name}"
        nodes.append(
            {
                "id": node_id,
                "label": f"{d.name}()",
                "file_type": "code",
                "source_file": path,
                "source_location": f"L{d.line}",
            }
        )
        edges.append(_edge(file_id, node_id, "contains", path, d.line))
    for call in structure.calls:
        if call.name in structure.assigned or call.name == stem:
            continue
        target = f"{file_id}_{call.name}" if call.name in defined else call.name
        edges.append(_edge(file_id, target, "calls", path, call.line))
    return {"nodes": nodes, "edges": edges}


def _node_id(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_").lower()


def _edge(source: str, target: str, relation: str, path: str, line: int) -> dict[str, Any]:
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "confidence": "EXTRACTED",
        "confidence_score": 1.0,
        "source_file": path,
        "source_location": f"L{line}",
        "weight": 1.0,
    }


# ----- OPC packages (.slx, .mlx, .slxc) and OPC text packages (.mdl) --------------------------


@dataclass
class Package:
    content_type: str | None = None
    release: str | None = None
    uuid: str | None = None
    revision: str | None = None
    created: str | None = None
    modified: str | None = None
    version: str | None = None
    model_name: str | None = None
    parts: list[str] = field(default_factory=list)
    model_references: list[str] = field(default_factory=list)
    library_links: list[str] = field(default_factory=list)
    variables_read: list[str] = field(default_factory=list)
    variables_written: list[str] = field(default_factory=list)
    code: str | None = None  # a live script's code
    problems: list[str] = field(default_factory=list)


def _tag(xml: str, name: str) -> str | None:
    found = re.search(rf"<(?:\w+:)?{name}\b[^>]*>([^<]*)</(?:\w+:)?{name}>", xml)
    return found.group(1).strip() if found else None


def _metadata(parts: dict[str, bytes], package: Package) -> None:
    def text(name: str) -> str:
        return parts.get(name, b"").decode("utf-8", "replace")

    core = text("metadata/coreProperties.xml")
    mw = text("metadata/mwcoreProperties.xml")
    package.content_type = _tag(mw, "contentType")
    package.release = _tag(mw, "matlabRelease") or _tag(core, "version")
    package.uuid = _tag(text("metadata/mwcorePropertiesExtension.xml"), "uuid")
    package.revision = _tag(core, "revision")
    package.created = _tag(core, "created")
    package.modified = _tag(core, "modified")
    package.version = _tag(text("metadata/mwcorePropertiesReleaseInfo.xml"), "version")


def safe_xml(data: bytes) -> ElementTree.Element:
    """Parse XML from an adopted file. A document type or entity declaration is refused before
    parsing (adopted files are untrusted: no entity expansion, no external resources); expat
    resolves no external entities by default."""
    _refuse_declarations(data)
    parser = ElementTree.XMLParser(encoding="utf-8")  # noqa: S314 - declarations refused above
    parser.feed(data)
    return parser.close()  # DTDs, entities and other encodings refused above


def _refuse_declarations(data: bytes) -> None:
    """Refuse by parser events, not by searching bytes (review cp17-234-a F6: a UTF-16
    document hid its DOCTYPE from a byte search and expat expanded its entities). The document
    is read as UTF-8 whatever it declares; another declared encoding, a byte-order mark of
    another encoding, a document type or an entity declaration is refused."""
    from xml.parsers import expat

    def refuse(*_: Any) -> None:
        raise ElementTree.ParseError("document type or entity declarations are refused")

    def declaration(_version: str | None, encoding: str | None, _standalone: int) -> None:
        if encoding is not None and encoding.lower().replace("_", "-") not in ("utf-8", "utf8"):
            raise ElementTree.ParseError("only UTF-8 XML is read")

    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in data[:4]:
        raise ElementTree.ParseError("only UTF-8 XML is read")
    checker = expat.ParserCreate("UTF-8")
    checker.StartDoctypeDeclHandler = refuse
    checker.EntityDeclHandler = refuse
    checker.UnparsedEntityDeclHandler = refuse
    checker.ExternalEntityRefHandler = refuse  # type: ignore[assignment]
    checker.XmlDeclHandler = declaration
    try:
        checker.Parse(data, True)
    except expat.ExpatError as exc:
        error = ElementTree.ParseError(str(exc))
        error.code = exc.code
        raise error from exc


def _blocks(xml: bytes, package: Package) -> None:
    try:
        root = safe_xml(xml)
    except ElementTree.ParseError as exc:
        package.problems.append(f"system XML did not parse ({getattr(exc, 'code', 'refused')})")
        return
    for block in root.iter("Block"):
        kind = block.get("BlockType")
        params = {p.get("Name"): (p.text or "").strip() for p in block.findall("P")}
        if kind == "ModelReference" and params.get("ModelNameDialog"):
            package.model_references.append(params["ModelNameDialog"])
        elif kind == "ModelReference" and params.get("ModelName"):
            package.model_references.append(params["ModelName"])
        if params.get("SourceBlock"):
            package.library_links.append(params["SourceBlock"])
        if kind == "FromWorkspace" and params.get("VariableName"):
            package.variables_read.append(params["VariableName"])
        if kind == "ToWorkspace" and params.get("VariableName"):
            package.variables_written.append(params["VariableName"])


def _model_parts(parts: dict[str, bytes], package: Package) -> None:
    diagram = parts.get("simulink/blockdiagram.xml", b"")
    named = re.search(rb'<Model\b[^>]*\bName="([^"]{1,200})"', diagram)
    if named:
        package.model_name = named.group(1).decode("utf-8", "replace")
    for name in sorted(parts):
        if name.startswith("simulink/systems/system_") and name.endswith(".xml"):
            _blocks(parts[name], package)
        elif name == "simulink/blockdiagram.xml" and b"<Block " in parts[name]:
            _blocks(parts[name], package)  # older releases keep blocks in the diagram part
    if "info/masterInformation.xml" in parts:
        package.model_name = _tag(
            parts["info/masterInformation.xml"].decode("utf-8", "replace"), "modelName"
        )
    if "matlab/document.xml" in parts:
        package.code = _live_code(parts["matlab/document.xml"])


def _live_code(document: bytes) -> str:
    """The code paragraphs of a live script, joined by blank lines."""
    try:
        root = safe_xml(document)
    except ElementTree.ParseError:
        return ""
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    chunks = []
    for paragraph in root.iter(f"{w}p"):
        style = paragraph.find(f"{w}pPr/{w}pStyle")
        if style is not None and style.get(f"{w}val") == "code":
            chunks.append("".join(t.text or "" for t in paragraph.iter(f"{w}t")))
    return "\n\n".join(chunks)


_WANTED = (
    "metadata/",
    "simulink/blockdiagram.xml",
    "simulink/systems/",
    "info/masterInformation.xml",
    "matlab/document.xml",
)


def read_package(data: bytes) -> Package:
    """An OPC package from its bytes (only metadata and model parts are read, each capped)."""
    package = Package()
    parts: dict[str, bytes] = {}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                package.parts.append(info.filename)
                if not info.filename.startswith(_WANTED) or info.filename.endswith(".png"):
                    continue
                if info.file_size > MAX_PART_BYTES:
                    package.problems.append(f"part over the size limit: {info.filename}")
                    continue
                if total + info.file_size > MAX_PACKAGE_BYTES:
                    package.problems.append("parts over the package budget; the rest not read")
                    break
                with archive.open(info) as part:
                    parts[info.filename] = part.read(MAX_PART_BYTES + 1)[:MAX_PART_BYTES]
                total += len(parts[info.filename])
    except (zipfile.BadZipFile, ValueError, OSError, EOFError, RuntimeError) as exc:
        package.problems.append(f"package did not open ({type(exc).__name__})")
        return package
    _metadata(parts, package)
    _model_parts(parts, package)
    return package


_PART = re.compile(r"^__MWOPC_PART_BEGIN__ (/\S+)\s*$", re.MULTILINE)


def read_mdl(text: str) -> Package:
    """A ``.mdl`` model: the MathWorks OPC Text Package of recent releases (XML parts between
    ``__MWOPC_PART_BEGIN__`` markers), or the classic ``Model {`` block syntax."""
    package = Package()
    if text.startswith("# MathWorks OPC Text Package"):
        marks = list(_PART.finditer(text))
        parts: dict[str, bytes] = {}
        for i, mark in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else text.find("__MWOPC_PACKAGE_END__")
            body = text[mark.end() : end if end != -1 else len(text)].strip("\r\n")
            name = mark.group(1).lstrip("/")
            package.parts.append(name)
            if name.startswith(_WANTED):
                parts[name] = body.encode("utf-8")
        _metadata(parts, package)
        _model_parts(parts, package)
        release = re.search(r"^__MWOPC_PACKAGE_BEGIN__ (\S+)", text, re.MULTILINE)
        if release and not package.release:
            package.release = release.group(1)
        return package
    if not re.match(r"\s*(?:#.*\n)*\s*(Model|Library)\s*\{", text):
        package.problems.append("not a Simulink model text")
        return package
    name = re.search(r'^\s*Name\s+"([^"]+)"', text, re.MULTILINE)
    package.model_name = name.group(1) if name else None
    package.release = (re.search(r"Simulink model saved in (R\d{4}[ab])", text) or [None, None])[1]
    for block in re.finditer(r"Block\s*\{(.*?)\n\s*\}", text, re.DOTALL):
        body = block.group(1)
        kind = re.search(r"BlockType\s+(\w+)", body)
        params = dict(re.findall(r'^\s*(\w+)\s+"([^"]*)"', body, re.MULTILINE))
        if kind and kind.group(1) == "ModelReference":
            target = params.get("ModelNameDialog") or params.get("ModelName")
            if target:
                package.model_references.append(target)
        if params.get("SourceBlock"):
            package.library_links.append(params["SourceBlock"])
        if kind and kind.group(1) == "FromWorkspace" and params.get("VariableName"):
            package.variables_read.append(params["VariableName"])
        if kind and kind.group(1) == "ToWorkspace" and params.get("VariableName"):
            package.variables_written.append(params["VariableName"])
    return package


def extract_simulink(path: str, data: bytes) -> dict[str, Any]:
    """Graphify-shaped extraction of one model (``.slx`` bytes or ``.mdl`` text): a model
    node, ``references`` edges to referenced models and ``imports_from`` edges to library
    blocks (EXTRACTED)."""
    package = (
        read_mdl(data.decode("utf-8", "replace"))
        if path.lower().endswith(".mdl")
        else read_package(data)
    )
    file_id = _node_id(path)
    node = {
        "id": file_id,
        "label": package.model_name or path.rsplit("/", 1)[-1],
        "file_type": "code",
        "source_file": path,
        "source_location": "L1",
    }
    edges = [_edge(file_id, ref, "references", path, 1) for ref in package.model_references]
    edges += [_edge(file_id, lib, "imports_from", path, 1) for lib in package.library_links]
    return {"nodes": [node], "edges": edges}


# ----- MAT-files ------------------------------------------------------------------------------


@dataclass
class MatHeader:
    version: str  # "v4", "v5", "v7.3"
    text: str | None = None
    platform: str | None = None
    created: str | None = None  # UTC-naive ISO from the header text, as written
    variables: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


_CREATED = re.compile(r"Created on: (\w{3} \w{3} +\d{1,2} \d{2}:\d{2}:\d{2} \d{4})")
_CLASSES = {
    1: "cell",
    2: "struct",
    3: "object",
    4: "char",
    5: "sparse",
    6: "double",
    7: "single",
    8: "int8",
    9: "uint8",
    10: "int16",
    11: "uint16",
    12: "int32",
    13: "uint32",
    14: "int64",
    15: "uint64",
}


def read_mat_header(data: bytes) -> MatHeader:
    """The header and top-level variable directory (name, class, dimensions) of a MAT-file,
    without reading variable data beyond what the directory needs. Compressed elements are
    inflated only up to a small cap."""
    if data.startswith(b"MATLAB 7.3 MAT-file") or data[512:520] == b"\x89HDF\r\n\x1a\n":
        header = MatHeader("v7.3", text=data[:116].decode("latin-1").rstrip("\x00 "))
        _header_text(header)
        return header
    if not data.startswith((b"MATLAB 5.0 MAT-file", b"MATLAB 7.0 MAT-file")):
        return MatHeader("v4", problems=["no level-5 header (MAT v4 or not a MAT-file)"])
    header = MatHeader("v5", text=data[:116].decode("latin-1").rstrip("\x00 "))
    _header_text(header)
    endian = data[126:128]
    fmt = "<" if endian == b"IM" else ">"
    offset = 128
    count = 0
    while offset + 8 <= len(data) and count < MAX_MAT_ELEMENTS:
        dtype, nbytes = struct.unpack(f"{fmt}II", data[offset : offset + 8])
        body = data[offset + 8 : offset + 8 + nbytes]
        compressed = dtype == 15  # miCOMPRESSED: no padding after it (review cp17-234-a F13)
        if compressed:
            try:
                inflater = zlib.decompressobj()
                body = inflater.decompress(body, MAX_DECOMPRESSED_HEADER)
                if len(body) >= 8:
                    dtype, inner = struct.unpack(f"{fmt}II", body[:8])
                    body = body[8 : 8 + inner]
            except zlib.error:
                header.problems.append("a compressed element did not inflate")
                break
        if dtype == 14:
            variable = _matrix_directory(body, fmt)
            if variable is not None:
                header.variables.append(variable)
        offset += 8 + nbytes + (0 if compressed else (-nbytes) % 8)
        count += 1
    return header


def _header_text(header: MatHeader) -> None:
    text = header.text or ""
    platform = re.search(r"Platform: ([^,]+),", text)
    header.platform = platform.group(1).strip() if platform else None
    created = _CREATED.search(text)
    if created:
        try:
            moment = datetime.strptime(" ".join(created.group(1).split()), "%a %b %d %H:%M:%S %Y")
            # The writing machine's local time, no time zone: comparable only between files
            # written on the same platform (fusion R7).
            header.created = moment.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            header.problems.append("unreadable creation time")


def _matrix_directory(body: bytes, fmt: str) -> dict[str, Any] | None:
    """Class, dimensions and name of one miMATRIX element (its first three sub-elements)."""
    pos = 0
    fields: list[bytes] = []
    for _ in range(3):
        if pos + 8 > len(body):
            return None
        small = struct.unpack(f"{fmt}I", body[pos : pos + 4])[0]
        if small >> 16:  # small data element: type in low 16 bits, size in high 16 bits
            size = small >> 16
            fields.append(body[pos + 4 : pos + 4 + size])
            pos += 8
        else:
            size = struct.unpack(f"{fmt}I", body[pos + 4 : pos + 8])[0]
            fields.append(body[pos + 8 : pos + 8 + size])
            pos += 8 + size + ((-size) % 8)
    flags, dims, name = fields
    if len(flags) < 4 or len(dims) < 8:
        return None
    klass = _CLASSES.get(struct.unpack(f"{fmt}I", flags[:4])[0] & 0xFF, "unknown")
    shape = list(struct.unpack(f"{fmt}{len(dims) // 4}i", dims[: len(dims) // 4 * 4]))
    return {"name": name.decode("latin-1"), "class": klass, "dims": shape}


# ----- MATLAB projects ------------------------------------------------------------------------


@dataclass
class ProjectEntry:
    path: str  # relative to the project root
    label: str | None


@dataclass
class ProjectInfo:
    name: str | None = None
    kind: str = "fixedPathV2"
    entries: list[ProjectEntry] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


_INFO = re.compile(rb"<Info\b([^>]*)/?>")
_ATTR = re.compile(rb'(\w+)="([^"]*)"')


def _attrs(data: bytes) -> dict[str, str]:
    found = _INFO.search(data)
    if not found:
        return {}
    return {k.decode(): v.decode("utf-8", "replace") for k, v in _ATTR.findall(found.group(1))}


def read_project(files: dict[str, bytes]) -> ProjectInfo:
    """A MATLAB project's file list and labels from its ``resources/project`` files (paths
    relative to that folder, contents as bytes), fixedPathV2 layout: the ``root`` folder's
    ``<key>p.xml`` with ``type="Files"`` names the files tree; inside it each child ``<key>``
    has ``<key>p.xml`` (its name, ``location``) and ``<key>d.xml`` (its data); a folder is the
    directory named by its key."""
    info = ProjectInfo()
    project = files.get("Project.xml", b"")
    kind = _attrs(project).get("MetadataType")
    if kind:
        info.kind = kind
    if info.kind != "fixedPathV2":
        info.problems.append(f"project metadata type {info.kind!r} is not read")
        return info
    roots = {name: _attrs(data) for name, data in files.items() if name.startswith("root/")}
    files_key = next(
        (n[5:-5] for n, a in roots.items() if n.endswith("p.xml") and a.get("type") == "Files"),
        None,
    )
    for name, attrs in roots.items():
        if name.endswith("d.xml") and attrs.get("Name"):
            info.name = attrs["Name"]
    if files_key is None:
        info.problems.append("no Files tree in the project metadata")
        return info

    def visit(key: str, prefix: str, depth: int) -> None:
        if depth > 64:
            info.problems.append("project folders nested too deeply")
            return
        children = sorted(
            {n[len(key) + 1 : -5] for n in files if n.startswith(key + "/") and n.endswith("p.xml")}
        )
        for child in children:
            attrs = _attrs(files[f"{key}/{child}p.xml"])
            location = attrs.get("location")
            if not location or attrs.get("type") != "File":
                continue
            rel = f"{prefix}{location}"
            if any(n.startswith(f"{child}/") for n in files):
                visit(child, rel + "/", depth + 1)
                continue
            label = re.search(rb'<Label UUID="([^"]+)"', files.get(f"{key}/{child}d.xml", b""))
            info.entries.append(ProjectEntry(rel, label.group(1).decode() if label else None))

    visit(files_key, "", 0)
    return info
