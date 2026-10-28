"""詞典版本：設定檔一改就產生新版本，舊版本可完整還原。"""
import shutil

import pytest

from radar import db
from radar.analysis import lexicon


@pytest.fixture
def lexfile(tmp_path):
    p = tmp_path / "lexicon.json"
    shutil.copy(lexicon.LEXICON_PATH, p)
    return p


@pytest.fixture
def conn():
    return db.connect(":memory:")


def test_first_call_creates_version_1(conn, lexfile):
    lex = lexicon.current(conn, lexfile)
    assert lex.version == 1
    assert lexicon.current(conn, lexfile).version == 1  # 沒改就不會多一版


def test_old_version_restores_exactly(conn, lexfile):
    v1 = lexicon.current(conn, lexfile)
    v2 = lexicon.set_term(conn, "漲價效應", 1.5, lexfile)
    assert v2.version == 2 and v2.sentiment["漲價效應"] == 1.5
    old = lexicon.get(conn, 1)
    assert old == v1 and list(old.sentiment) == list(v1.sentiment)  # 內容與順序都一樣
    assert "漲價效應" not in old.sentiment


def test_set_existing_term_keeps_position(conn, lexfile):
    lexicon.current(conn, lexfile)
    before = list(lexicon.load_file(lexfile).sentiment)
    lex = lexicon.set_term(conn, "利空", -3, lexfile)
    assert list(lex.sentiment) == before and lex.sentiment["利空"] == -3


def test_remove_term(conn, lexfile):
    lexicon.current(conn, lexfile)
    lex = lexicon.remove_term(conn, "看好", lexfile)
    assert "看好" not in lex.sentiment and lex.version == 2
    assert "看好" in lexicon.get(conn, 1).sentiment
    with pytest.raises(KeyError):
        lexicon.remove_term(conn, "看好", lexfile)


def test_file_round_trip_is_lossless(lexfile):
    lex = lexicon.load_file(lexfile)
    lexicon.save_file(lex, lexfile)
    assert lexicon.load_file(lexfile).checksum() == lex.checksum()
    assert "_說明" in lexfile.read_text(encoding="utf-8")


def test_hand_edited_file_becomes_new_version(conn, lexfile):
    lexicon.current(conn, lexfile)
    lexfile.write_text(lexfile.read_text(encoding="utf-8").replace('"創新高": 2.5', '"創新高": 3'), encoding="utf-8")
    lex = lexicon.current(conn, lexfile)
    assert lex.version == 2 and lex.sentiment["創新高"] == 3


@pytest.mark.parametrize("w", [0, 6, -5.5, float("nan")])
def test_rejects_bad_weight(conn, lexfile, w):
    with pytest.raises(ValueError):
        lexicon.set_term(conn, "某詞", w, lexfile)


def test_unknown_version(conn):
    with pytest.raises(KeyError):
        lexicon.get(conn, 99)
