#!/usr/bin/env python3
"""Render a PlantUML .puml file to a real image file.

Encodes the diagram source (deflate + PlantUML's custom base64) and fetches the
already-rendered image from the public PlantUML server, then writes the bytes to
disk. The user never has to convert anything by hand.

This is a faithful port of the encoding used by the original AI DevStudio web app
(PlantUMLRenderer.tsx: pako.deflate level 9 -> custom base64 -> "~1" URL prefix).

Standard library only (zlib + urllib). No pip install required.

CJK note: the public PlantUML server intermittently drops CJK (Chinese/Japanese/
Korean) glyphs when rasterizing PNG (~25% of renders, position varies). SVG is
never affected (text is embedded as <text>, not rasterized server-side). For PNG
this script self-corrects: it fetches several independent renders (each with a
cache-bust marker) and keeps the largest/most-complete one (--attempts, default
4). This is probabilistic mitigation; use --format svg for a guaranteed copy.

Usage:
    python render_puml.py <input.puml> [--format png|svg] [--output <path>]
                          [--server <url>] [--timeout <seconds>] [--print-url]
                          [--attempts <N>]

Exit codes:
    0  success (image written)
    1  usage / file error
    2  render / network error (the .puml is kept; nothing was overwritten)
"""
import argparse
import os
import re
import sys
import zlib
import urllib.request
import urllib.error

DEFAULT_SERVER = "https://www.plantuml.com/plantuml"


def encode6bit(b: int) -> str:
    if b < 10:
        return chr(48 + b)
    b -= 10
    if b < 26:
        return chr(65 + b)
    b -= 26
    if b < 26:
        return chr(97 + b)
    b -= 26
    if b == 0:
        return "-"
    if b == 1:
        return "_"
    return "?"


def append3bytes(b1: int, b2: int, b3: int) -> str:
    c1 = b1 >> 2
    c2 = ((b1 & 0x3) << 4) | (b2 >> 4)
    c3 = ((b2 & 0xF) << 2) | (b3 >> 6)
    c4 = b3 & 0x3F
    return (
        encode6bit(c1 & 0x3F)
        + encode6bit(c2 & 0x3F)
        + encode6bit(c3 & 0x3F)
        + encode6bit(c4 & 0x3F)
    )


def encode64(data: bytes) -> str:
    r = []
    n = len(data)
    i = 0
    while i < n:
        if i + 2 < n:
            r.append(append3bytes(data[i], data[i + 1], data[i + 2]))
        elif i + 1 < n:
            r.append(append3bytes(data[i], data[i + 1], 0))
        else:
            r.append(append3bytes(data[i], 0, 0))
        i += 3
    return "".join(r)


def clean_source(code: str) -> str:
    """Mirror the web app's cleanup: prefer the @startuml..@enduml block,
    strip stray markdown fences, and ensure the start/end markers exist."""
    match = re.search(r"@startuml[\s\S]*?@enduml", code)
    if match:
        return match.group(0)
    clean = code.strip()
    if clean.startswith("```"):
        lines = clean.split("\n")
        clean = "\n".join(lines[1:-1]).strip()
    if not clean.startswith("@startuml"):
        clean = "@startuml\n" + clean
    if not clean.endswith("@enduml"):
        clean = clean + "\n@enduml"
    return clean


def inject_cachebust(source: str, marker: int) -> str:
    """Insert a no-op PlantUML comment right after @startuml so the encoded
    URL differs (busts the server cache) WITHOUT changing the rendered image.
    `'` begins a single-line comment in PlantUML, so this is visually inert."""
    if marker <= 0:
        return source
    return source.replace("@startuml", f"@startuml\n' cachebust {marker}", 1)


def encode_plantuml(code: str, marker: int = 0) -> str:
    source = clean_source(code)
    if marker:
        source = inject_cachebust(source, marker)
    raw = source.encode("utf-8")
    compressed = zlib.compress(raw, 9)
    return encode64(compressed)


def build_url(server: str, fmt: str, encoded: str) -> str:
    return f"{server.rstrip('/')}/{fmt}/~1{encoded}"


def fetch_url(url: str, timeout: float) -> bytes:
    """Fetch a URL and return the body bytes (raises on HTTP/URL error)."""
    req = urllib.request.Request(url, headers={"User-Agent": "prd-sdd-studio/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_png_selfcorrecting(server: str, code: str, timeout: float,
                             attempts: int) -> tuple:
    """Self-correction for the PlantUML server's intermittent CJK glyph-drop
    when rasterizing PNG: fetch up to `attempts` independent renders (each with
    a distinct cache-bust marker so the server re-renders rather than returning
    a cached copy) and keep the LARGEST result. A complete render has more black
    glyph pixels than one that silently dropped characters, so it is reliably
    larger in bytes. This drives the ~25% per-render drop rate down to ~0.25^N.

    Returns (best_bytes, sizes_list). Stdlib only — it cannot verify glyphs
    pixel-by-pixel (no image lib), so this is probabilistic mitigation; SVG is
    the only 100%-correct format.
    """
    best = b""
    sizes = []
    for i in range(max(1, attempts)):
        encoded = encode_plantuml(code, marker=i)
        url = build_url(server, "png", encoded)
        data = fetch_url(url, timeout)
        sizes.append(len(data))
        if len(data) > len(best):
            best = data
    return best, sizes


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a .puml file to an image.")
    parser.add_argument("input", help="Path to the .puml source file")
    parser.add_argument("--format", default="png", choices=["png", "svg"],
                        help="Output image format (default: png)")
    parser.add_argument("--output", help="Output image path (default: alongside input)")
    parser.add_argument("--server", default=DEFAULT_SERVER,
                        help="PlantUML server base URL")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="Network timeout in seconds (default: 30)")
    parser.add_argument("--print-url", action="store_true",
                        help="Also print the encoded server URL")
    parser.add_argument("--attempts", type=int, default=4,
                        help="PNG self-correction: fetch N independent renders "
                             "and keep the largest/most-complete (default: 4). "
                             "Mitigates the server's intermittent CJK glyph-drop. "
                             "Ignored for SVG (always correct).")
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"ERROR: input file not found: {args.input}", file=sys.stderr)
        return 1

    try:
        with open(args.input, "r", encoding="utf-8") as fh:
            code = fh.read()
    except OSError as exc:
        print(f"ERROR: cannot read {args.input}: {exc}", file=sys.stderr)
        return 1

    if not code.strip():
        print(f"ERROR: {args.input} is empty", file=sys.stderr)
        return 1

    encoded = encode_plantuml(code)
    url = build_url(args.server, args.format, encoded)
    if args.print_url:
        print(url)

    output = args.output or (os.path.splitext(args.input)[0] + "." + args.format)

    try:
        if args.format == "png" and args.attempts > 1:
            # Self-correcting fetch: the server intermittently drops CJK glyphs
            # when rasterizing PNG. Fetch several independent renders and keep
            # the largest (most complete). See fetch_png_selfcorrecting().
            data, sizes = fetch_png_selfcorrecting(
                args.server, code, args.timeout, args.attempts)
            if len(set(sizes)) > 1:
                print(f"NOTE: PNG self-correction - render sizes {sizes}, "
                      f"kept largest ({len(data)} bytes) as most-complete. "
                      f"(For a guaranteed-correct copy, use --format svg.)")
        else:
            data = fetch_url(url, args.timeout)
    except urllib.error.HTTPError as exc:
        print(f"ERROR: PlantUML server returned HTTP {exc.code}. "
              f"The diagram source likely has a syntax error. "
              f"The .puml file was kept at {args.input}.", file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"ERROR: could not reach PlantUML server ({exc}). "
              f"Check your network. The .puml file was kept at {args.input}.",
              file=sys.stderr)
        return 2

    if not data:
        print("ERROR: server returned an empty response.", file=sys.stderr)
        return 2

    try:
        with open(output, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        print(f"ERROR: cannot write {output}: {exc}", file=sys.stderr)
        return 1

    print(f"OK: wrote {output} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
