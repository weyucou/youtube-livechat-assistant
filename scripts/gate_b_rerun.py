"""Gate B re-run: NL-only rows through gemma4:e4b via Ollama + yt-dlp search.

Bypasses YOUTUBE_API_KEY by using yt-dlp ytsearch3: for candidate lookup.
Reuses SuperChat extraction and tool-calling logic from resolver_bench.py.

Usage:
    uv run scripts/gate_b_rerun.py <VOD_URL> [<VOD_URL> ...]
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openai

VOD_URL_PATTERN = re.compile(
    r"^https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?|live/|shorts/)|youtu\.be/)[A-Za-z0-9_\-?&=/.]+$"
)
URL_ID_PATTERN = re.compile(
    r"(?:v=|youtu\.be/|youtube\.com/watch\?v=|youtube\.com/shorts/|youtube\.com/embed/)"
    r"([A-Za-z0-9_-]{11})"
)
STANDALONE_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")

OSS_MODEL = "gemma4:e4b"
OSS_BASE_URL = "http://localhost:11434/v1"
RESOLVE_TOOL_NAME = "resolve_music_request"
OPENAI_TOOL = {
    "type": "function",
    "function": {
        "name": RESOLVE_TOOL_NAME,
        "description": (
            "Parse a live-chat message to identify a music track request. "
            "Return the artist, track title, a confidence score, and a fallback search query."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artist": {"type": "string", "description": "Artist name or empty string."},
                "track": {"type": "string", "description": "Track title or empty string."},
                "confidence": {"type": "number", "description": "Confidence 0.0-1.0."},
                "fallback_query": {"type": "string", "description": "YouTube search query."},
            },
            "required": ["artist", "track", "confidence", "fallback_query"],
        },
    },
}
SYSTEM_PROMPT = textwrap.dedent("""\
    You are a music-request parser for a YouTube reaction streamer.
    Viewer messages are SuperChat donations asking the streamer to react to a song.
    Extract the artist and track title. If no music request, return empty strings
    for artist and track with confidence 0.
""")

CURRENCY_PREFIXES = (
    ("CA$", "CAD"), ("AU$", "AUD"), ("A$", "AUD"), ("HK$", "HKD"),
    ("NZ$", "NZD"), ("MX$", "MXN"), ("NT$", "TWD"), ("R$", "BRL"),
    ("S$", "SGD"), ("€", "EUR"), ("£", "GBP"), ("¥", "JPY"),
    ("₹", "INR"), ("₩", "KRW"), ("₱", "PHP"), ("฿", "THB"),
    ("₽", "RUB"), ("₺", "TRY"), ("$", "USD"),
)


@dataclass
class SuperChat:
    text: str
    author: str


@dataclass
class Resolved:
    artist: str
    track: str
    confidence: float
    fallback_query: str


@dataclass
class Candidate:
    video_id: str
    title: str
    channel: str

    def __str__(self) -> str:
        t = self.title[:30] if len(self.title) > 30 else self.title
        return f"[{self.video_id}] {t}"


@dataclass
class NLRow:
    superchat: SuperChat
    resolved: Resolved | None = None
    candidates: list[Candidate] = field(default_factory=list)
    error: str = ""


def parse_amount_text(text: str) -> tuple[float | None, str | None]:
    s = text.strip()
    for prefix, iso in CURRENCY_PREFIXES:
        if s.startswith(prefix):
            numeric = s[len(prefix):].strip().replace(",", "")
            try:
                return float(numeric), iso
            except ValueError:
                return None, iso
    return None, None


def run_yt_dlp(vod_url: str, out_dir: Path) -> Path:
    if not VOD_URL_PATTERN.match(vod_url):
        raise ValueError(f"Not a YouTube URL: {vod_url!r}")
    result = subprocess.run(
        ["yt-dlp", "--skip-download", "--write-subs", "--sub-langs", "live_chat",
         "--sub-format", "json", "-o", str(out_dir / "chat.%(ext)s"), vod_url],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp error:\n{result.stderr}")
    matches = list(out_dir.glob("*.live_chat.json"))
    if not matches:
        raise RuntimeError("yt-dlp produced no live_chat.json")
    return matches[0]


def fetch_superchats(vod_url: str) -> list[SuperChat]:
    with tempfile.TemporaryDirectory() as tmpdir:
        sub_file = run_yt_dlp(vod_url, Path(tmpdir))
        out: list[SuperChat] = []
        with sub_file.open() as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                replay = entry.get("replayChatItemAction")
                if not replay:
                    continue
                for action_item in replay.get("actions", []):
                    renderer = (
                        action_item.get("addChatItemAction", {})
                        .get("item", {})
                        .get("liveChatPaidMessageRenderer")
                    )
                    if not renderer:
                        continue
                    runs = renderer.get("message", {}).get("runs") or []
                    text = "".join(r.get("text", "") for r in runs).strip()
                    if text:
                        out.append(SuperChat(
                            text=text,
                            author=renderer.get("authorName", {}).get("simpleText", ""),
                        ))
        return out


def extract_video_id(text: str) -> str | None:
    m = URL_ID_PATTERN.search(text)
    if m:
        return m.group(1)
    for m in STANDALONE_ID_PATTERN.finditer(text):
        token = m.group(1)
        if any(c.isdigit() or c in "_-" for c in token):
            return token
    return None


def resolve_llm(text: str, client: openai.OpenAI) -> Resolved:
    resp = client.chat.completions.create(
        model=OSS_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        tools=[OPENAI_TOOL],
        tool_choice={"type": "function", "function": {"name": RESOLVE_TOOL_NAME}},
    )
    choice = resp.choices[0]
    if choice.message.tool_calls:
        args = json.loads(choice.message.tool_calls[0].function.arguments)
        return Resolved(
            artist=str(args.get("artist", "")),
            track=str(args.get("track", "")),
            confidence=float(args.get("confidence", 0.0)),
            fallback_query=str(args.get("fallback_query", "")),
        )
    return Resolved(artist="", track="", confidence=0.0, fallback_query="")


def search_ytdlp(resolved: Resolved, max_results: int = 3) -> list[Candidate]:
    query = f"{resolved.artist} {resolved.track}".strip() or resolved.fallback_query
    if not query:
        return []
    search_url = f"ytsearch{max_results}:{query}"
    result = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--print", "%(id)s\t%(title)s\t%(channel)s", search_url],
        capture_output=True, text=True, check=False,
    )
    candidates = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 2)
        if len(parts) >= 2:
            candidates.append(Candidate(
                video_id=parts[0],
                title=parts[1],
                channel=parts[2] if len(parts) > 2 else "",
            ))
    return candidates


def process_row(sc: SuperChat, client: openai.OpenAI) -> NLRow:
    row = NLRow(superchat=sc)
    try:
        row.resolved = resolve_llm(sc.text, client)
        row.candidates = search_ytdlp(row.resolved)
    except Exception as exc:  # noqa: BLE001
        row.error = str(exc)
    return row


def main() -> None:
    vod_urls = sys.argv[1:]
    if not vod_urls:
        sys.exit("Usage: uv run scripts/gate_b_rerun.py <VOD_URL> [<VOD_URL> ...]")

    client = openai.OpenAI(api_key="ollama", base_url=OSS_BASE_URL)

    all_nl_rows: list[NLRow] = []

    for vod_url in vod_urls:
        print(f"\nFetching SuperChats from {vod_url} ...", flush=True)
        try:
            superchats = fetch_superchats(vod_url)
        except Exception as exc:
            print(f"  ERROR: {exc}", flush=True)
            continue
        print(f"  Found {len(superchats)} SuperChats.", flush=True)

        nl_only = [sc for sc in superchats if not extract_video_id(sc.text)]
        print(f"  NL-only (no regex ID): {len(nl_only)}", flush=True)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(process_row, sc, client): sc for sc in nl_only}
            for i, fut in enumerate(as_completed(futures), 1):
                row = fut.result()
                all_nl_rows.append(row)
                print(f"  [{i}/{len(nl_only)}] {row.superchat.text[:60]!r}", flush=True)

    # Print Table B
    print("\n\n---\n\n### Table B — NL-only rows (LLM path, Gate B ≥80% top-3)\n")
    header = "| # | SuperChat text | LLM parsed (artist — track) | Conf | Candidate 1 | Candidate 2 | Candidate 3 |"
    sep    = "|---|---------------|----------------------------|------|-------------|-------------|-------------|"
    print(header)
    print(sep)
    for i, row in enumerate(all_nl_rows, 1):
        text = row.superchat.text[:50].replace("|", "\\|")
        if row.error:
            print(f"| {i} | {text} | ERROR: {row.error[:30]} | — | — | — | — |")
            continue
        r = row.resolved
        parsed = f"{r.artist} — {r.track}" if r and r.track else "(no music request)"
        conf = f"{r.confidence:.2f}" if r else "—"
        cands = [str(c) for c in row.candidates] + ["", "", ""]
        print(f"| {i} | {text} | {parsed[:35]} | {conf} | {cands[0][:35]} | {cands[1][:35]} | {cands[2][:35]} |")

    # Gate B scoring hint
    music_rows = [r for r in all_nl_rows if r.resolved and r.resolved.track]
    print(f"\n**Music-request rows identified by LLM:** {len(music_rows)} of {len(all_nl_rows)}")
    print("**Gate B:** Hand-score whether the correct video appears in Candidates 1–3.")
    print("**Target:** ≥80% correct-in-top-3 on music-request rows.")


if __name__ == "__main__":
    main()
