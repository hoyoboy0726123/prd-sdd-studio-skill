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

離線 / 受限環境：某些主機有出口封鎖或資料外流防護，會硬擋對 plantuml.com 的連線
（即使使用者同意也無法覆蓋）。本腳本因此支援離線出圖，優先順序：
    1. 本地 plantuml.jar（--jar / PLANTUML_JAR / scripts/plantuml.jar，需 Java）——完全不連網；
    2. --offline：不連網，若無本地 jar 則保留 .puml 並印出離線出圖指引；
    3. 對外伺服器（可用 PLANTUML_SERVER 指向內網伺服器）。
對外連線失敗時「保留 .puml + 給指引、不視為致命錯誤」——圖檔僅為選配，PRD/SDD/.puml 才是主產物。

Usage:
    python render_puml.py <input.puml> [--format png|svg] [--output <path>]
                          [--server <url>] [--timeout <seconds>] [--print-url]
                          [--attempts <N>] [--jar <plantuml.jar>] [--offline]

Exit codes:
    0  success (image written，或 --offline 下已保留 .puml)
    1  usage / file error
    2  對外渲染失敗（.puml 已保留，附離線出圖指引）
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import zlib
import urllib.request
import urllib.error

# 伺服器可用環境變數覆寫（例如指向內網 PlantUML 伺服器）。
DEFAULT_SERVER = os.environ.get("PLANTUML_SERVER", "https://www.plantuml.com/plantuml")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def find_local_jar(explicit: str | None) -> str | None:
    """尋找本地 plantuml.jar：--jar 參數 → PLANTUML_JAR 環境變數 → 腳本同目錄。"""
    for cand in (explicit, os.environ.get("PLANTUML_JAR"),
                 os.path.join(SCRIPT_DIR, "plantuml.jar")):
        if cand and os.path.isfile(cand):
            return cand
    return None


def render_local_jar(jar: str, input_path: str, fmt: str, output: str) -> bool:
    """用本地 plantuml.jar 離線渲染（需 Java）。成功回傳 True，否則 False。

    完全不連外網——適合有出口封鎖 / 資料外流防護的環境。
    """
    java = shutil.which("java")
    if not java:
        print("NOTE: 找到 plantuml.jar 但系統無 java，改用其他方式。", file=sys.stderr)
        return False
    try:
        # plantuml 會把圖輸出到 input 同目錄同名檔；再視需要搬到 output
        subprocess.run([java, "-jar", jar, f"-t{fmt}", input_path],
                       check=True, capture_output=True, timeout=60)
    except (subprocess.CalledProcessError, subprocess.TimeoutError, OSError) as exc:
        print(f"NOTE: 本地 plantuml.jar 渲染失敗（{exc}）。", file=sys.stderr)
        return False
    produced = os.path.splitext(input_path)[0] + "." + fmt
    if produced != output and os.path.isfile(produced):
        try:
            shutil.move(produced, output)
        except OSError:
            pass
    ok = os.path.isfile(output)
    if ok:
        print(f"OK (local): wrote {output}")
    return ok


def offline_hint(input_path: str) -> None:
    """對外渲染不可用時，告知使用者離線出圖選項（.puml 本身即為有效產物）。"""
    print(
        "\n圖檔未產生，但 PlantUML 原始碼已保留於：\n"
        f"  {input_path}\n"
        "此環境無法（或不允許）連線到 PlantUML 線上服務。可用離線方式出圖：\n"
        "  1) 安裝 Java 後，把 plantuml.jar 放到本 skill 的 scripts/ 目錄，重跑即可離線產圖；\n"
        "  2) VS Code 安裝「PlantUML」擴充套件，開啟 .puml 直接預覽/匯出；\n"
        "  3) 於可連網的機器手動把 .puml 內容貼到 plantuml.com。\n"
        "註：PRD/SDD 文件與 .puml 原始碼皆已完成，圖檔僅為選配。",
        file=sys.stderr,
    )


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
                        help="Output image format (default: png,好讀)。單次請求;失敗會自動退回 SVG。")
    parser.add_argument("--output", help="Output image path (default: alongside input)")
    parser.add_argument("--server", default=DEFAULT_SERVER,
                        help="PlantUML server base URL")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="Network timeout in seconds (default: 30)")
    parser.add_argument("--print-url", action="store_true",
                        help="Also print the encoded server URL")
    parser.add_argument("--attempts", type=int, default=1,
                        help="PNG 自我修正:抓 N 次獨立渲染取最完整,緩解伺服器間歇掉 CJK "
                             "(預設 1 = 單次請求,最不易觸發外流偵測)。設 >1 才啟用多次修正; "
                             "SVG 一律單次且永遠正確。")
    parser.add_argument("--jar", help="Path to local plantuml.jar for OFFLINE rendering "
                                       "(else PLANTUML_JAR env or scripts/plantuml.jar)")
    parser.add_argument("--offline", action="store_true",
                        help="Never call the network. Render with local plantuml.jar if "
                             "available; otherwise keep the .puml and print offline hints.")
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

    output = args.output or (os.path.splitext(args.input)[0] + "." + args.format)

    # 1) 離線優先：若有本地 plantuml.jar 就地渲染，完全不連外網。
    jar = find_local_jar(args.jar)
    if jar and render_local_jar(jar, args.input, args.format, output):
        return 0

    # 2) --offline 且無可用本地渲染 → 保留 .puml、給離線指引，不視為錯誤。
    if args.offline:
        offline_hint(args.input)
        return 0

    if args.print_url:
        print(build_url(args.server, args.format, encode_plantuml(code)))

    # 3) 對外渲染:正常「單次請求」。要 PNG 時,若那一次失敗 → 自動退回抓一次 SVG;
    #    連 SVG 也失敗(真的被封鎖)→ 保留 .puml + 離線指引,不阻斷主流程。
    def _fetch(fmt: str) -> bytes:
        if fmt == "png" and args.attempts > 1:
            # 僅在使用者明確要求多次修正時才連送多次(受限環境不建議)。
            data, sizes = fetch_png_selfcorrecting(args.server, code, args.timeout, args.attempts)
            if len(set(sizes)) > 1:
                print(f"NOTE: PNG 自我修正 - 各次大小 {sizes},取最大 {len(data)} bytes。")
            return data
        return fetch_url(build_url(args.server, fmt, encode_plantuml(code)), args.timeout)

    fmt, data = args.format, None
    try:
        data = _fetch(args.format)
    except urllib.error.HTTPError as exc:
        print(f"ERROR: PlantUML 伺服器回 HTTP {exc.code}(原始碼可能有語法錯)。.puml 已保留於 {args.input}。",
              file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError) as exc:
        # 首選格式(通常 PNG)連線失敗:先嘗試退回 SVG 一次。
        if args.format == "png":
            print(f"NOTE: PNG 請求失敗（{exc}）→ 改抓一次 SVG。", file=sys.stderr)
            fmt = "svg"
            output = args.output or (os.path.splitext(args.input)[0] + ".svg")
            try:
                data = fetch_url(build_url(args.server, "svg", encode_plantuml(code)), args.timeout)
            except (urllib.error.URLError, TimeoutError, urllib.error.HTTPError) as exc2:
                print(f"NOTE: SVG 也無法取得（{exc2}）。", file=sys.stderr)
                offline_hint(args.input)
                return 2
        else:
            print(f"NOTE: 無法連線到 PlantUML 服務（{exc}）。", file=sys.stderr)
            offline_hint(args.input)
            return 2

    if not data:
        print("ERROR: server returned an empty response.", file=sys.stderr)
        return 2

    if fmt != args.format:
        print(f"NOTE: 已改用 {fmt} 格式輸出。")
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
