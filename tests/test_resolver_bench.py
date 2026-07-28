"""Tests for scripts/resolver_bench.py helper functions."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from resolver_bench import (
    CANDIDATE_TITLE_MAX_LEN,
    BenchRow,
    Candidate,
    Resolved,
    SuperChat,
    _extract_superchat,
    print_llm_table,
    print_regex_table,
    search_youtube,
)

AMOUNT_VALUE = 10.0
AMOUNT_DISPLAY = "$10.00"


def make_superchat(text: str) -> SuperChat:
    return SuperChat(
        text=text,
        author="viewer",
        amount_display=AMOUNT_DISPLAY,
        amount_value=AMOUNT_VALUE,
        currency="USD",
        currency_ambiguous=True,
        timestamp_usec="1700000000000000",
        video_offset_ms="12000",
    )


def replay_entry(runs: list[dict], *, amount: str = AMOUNT_DISPLAY) -> dict:
    """A yt-dlp live_chat line carrying one paid message."""
    return {
        "replayChatItemAction": {
            "videoOffsetTimeMsec": "12000",
            "actions": [
                {
                    "addChatItemAction": {
                        "item": {
                            "liveChatPaidMessageRenderer": {
                                "message": {"runs": runs},
                                "purchaseAmountText": {"simpleText": amount},
                                "authorName": {"simpleText": "viewer"},
                                "timestampUsec": "1700000000000000",
                            }
                        }
                    }
                }
            ],
        }
    }


class TestExtractSuperchat:
    def test_single_run_message(self):
        entry = replay_entry([{"text": "can you react to Numb by Linkin Park"}])
        sc = _extract_superchat(entry)
        assert sc is not None
        assert sc.text == "can you react to Numb by Linkin Park"

    def test_joins_multiple_runs(self):
        # yt-dlp splits a message into runs around emotes; the text is the join.
        entry = replay_entry([{"text": "play "}, {"emoji": {}}, {"text": "Bohemian Rhapsody"}])
        sc = _extract_superchat(entry)
        assert sc is not None
        assert sc.text == "play Bohemian Rhapsody"

    def test_parses_amount_and_flags_ambiguous_currency(self):
        entry = replay_entry([{"text": "react to Hello"}], amount=AMOUNT_DISPLAY)
        sc = _extract_superchat(entry)
        assert sc is not None
        assert sc.amount_value == AMOUNT_VALUE
        assert sc.currency == "USD"
        assert sc.currency_ambiguous is True

    def test_non_replay_entry_returns_none(self):
        assert _extract_superchat({}) is None

    def test_non_paid_message_returns_none(self):
        entry = {
            "replayChatItemAction": {
                "videoOffsetTimeMsec": "12000",
                "actions": [{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {}}}}],
            }
        }
        assert _extract_superchat(entry) is None

    def test_empty_message_returns_none(self):
        assert _extract_superchat(replay_entry([{"text": "   "}])) is None


class TestSearchYoutube:
    def _make_yt_service(self, items: list[dict]) -> MagicMock:
        mock_svc = MagicMock()
        mock_svc.search().list().execute.return_value = {"items": items}
        return mock_svc

    def test_returns_candidates(self):
        items = [
            {"id": {"videoId": "abc123"}, "snippet": {"title": "Numb - Linkin Park", "channelTitle": "LP"}},
            {"id": {"videoId": "def456"}, "snippet": {"title": "Numb (Cover)", "channelTitle": "CoverCh"}},
        ]
        resolved = Resolved(artist="Linkin Park", track="Numb", confidence=0.9, fallback_query="")
        candidates = search_youtube(resolved, self._make_yt_service(items))
        assert len(candidates) == len(items)
        assert candidates[0].video_id == "abc123"
        assert candidates[0].title == "Numb - Linkin Park"

    def test_uses_fallback_query_when_no_artist_or_track(self):
        items = [{"id": {"videoId": "xyz"}, "snippet": {"title": "Song X", "channelTitle": "Ch"}}]
        resolved = Resolved(artist="", track="", confidence=0.2, fallback_query="numb linkin park reaction")
        mock_svc = self._make_yt_service(items)
        search_youtube(resolved, mock_svc)
        mock_svc.search().list.assert_called_with(
            part="snippet", q="numb linkin park reaction", maxResults=3, type="video"
        )

    def test_empty_query_returns_no_candidates(self):
        resolved = Resolved(artist="", track="", confidence=0.0, fallback_query="")
        candidates = search_youtube(resolved, MagicMock())
        assert candidates == []

    def test_missing_items_returns_empty(self):
        mock_svc = MagicMock()
        mock_svc.search().list().execute.return_value = {}
        resolved = Resolved(artist="Adele", track="Hello", confidence=0.8, fallback_query="")
        candidates = search_youtube(resolved, mock_svc)
        assert candidates == []


class TestPrintRegexTable:
    def test_prints_hydrated_row(self, capsys: pytest.CaptureFixture[str]):
        rows = [
            BenchRow(
                superchat=make_superchat("react to https://youtu.be/abc12345678"),
                resolver_path="regex",
                extracted_id="abc12345678",
                candidates=[Candidate("abc12345678", "Numb - Linkin Park", "LP")],
            )
        ]
        print_regex_table(rows)
        out = capsys.readouterr().out
        assert "abc12345678" in out
        assert "Numb - Linkin Park" in out

    def test_videos_list_miss_row(self, capsys: pytest.CaptureFixture[str]):
        rows = [
            BenchRow(
                superchat=make_superchat("react to https://youtu.be/deadbeef123"),
                resolver_path="regex",
                extracted_id="deadbeef123",
                candidates=[],
            )
        ]
        print_regex_table(rows)
        assert "(videos.list miss)" in capsys.readouterr().out


class TestPrintLlmTable:
    def test_prints_parsed_row(self, capsys: pytest.CaptureFixture[str]):
        rows = [
            BenchRow(
                superchat=make_superchat("can you react to Numb"),
                resolver_path="llm",
                resolved=Resolved(artist="Linkin Park", track="Numb", confidence=0.9, fallback_query=""),
                candidates=[Candidate("abc123", "Numb - Linkin Park", "LP")],
                model="claude-haiku-4-5-20251001",
            )
        ]
        print_llm_table(rows)
        out = capsys.readouterr().out
        assert "Numb" in out
        assert "Linkin Park" in out

    def test_no_match_row(self, capsys: pytest.CaptureFixture[str]):
        rows = [
            BenchRow(
                superchat=make_superchat("hello streamer!"),
                resolver_path="llm",
                resolved=Resolved(artist="", track="", confidence=0.0, fallback_query=""),
                candidates=[],
                model="claude-haiku-4-5-20251001",
            )
        ]
        print_llm_table(rows)
        assert "(no match)" in capsys.readouterr().out

    def test_unresolved_row_renders_no_match(self, capsys: pytest.CaptureFixture[str]):
        # resolved defaults to None when the LLM call failed; the table must not raise.
        rows = [BenchRow(superchat=make_superchat("???"), resolver_path="llm")]
        print_llm_table(rows)
        assert "(no match)" in capsys.readouterr().out


class TestCandidate:
    def test_str_truncates_long_title(self):
        c = Candidate("abc", "A" * 40, "Channel")
        result = str(c)
        assert result.endswith("...")
        assert len(result) < CANDIDATE_TITLE_MAX_LEN + 20  # id prefix + ellipsis overhead

    def test_str_short_title(self):
        c = Candidate("abc", "Short title", "Channel")
        assert str(c) == "[abc] Short title"
