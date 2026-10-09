"""shared/csvtail.py must return exactly what a whole-file read would, for the
part it is asked for."""
import pandas as pd
import pytest

from shared.csvtail import read_tail, last_ts_ms, CANDLE_COLUMNS
from tests.synth import synth_1h, scale_prices, SHIB_SCALE, BTC_SCALE


@pytest.fixture(params=[1.0, SHIB_SCALE, BTC_SCALE], ids=["sol", "shib", "btc"])
def csv_file(tmp_path, request):
    df = scale_prices(synth_1h(cycles=12), request.param)        # ~1560 rows
    p = tmp_path / "X_USDT_1h.csv"
    df.to_csv(p, index=False)
    return p, pd.read_csv(p)


def test_no_arguments_reads_everything(csv_file):
    p, full = csv_file
    pd.testing.assert_frame_equal(read_tail(p), full, check_dtype=False)


@pytest.mark.parametrize("n", [1, 8, 200, 1000, 5000])
def test_rows_matches_full_read_tail(csv_file, n):
    p, full = csv_file
    got = read_tail(p, rows=n)
    assert len(got) >= min(n, len(full))
    pd.testing.assert_frame_equal(got.tail(n).reset_index(drop=True),
                                  full.tail(n).reset_index(drop=True), check_dtype=False)


def test_small_chunk_grows_until_satisfied(csv_file, monkeypatch):
    import shared.csvtail as ct
    monkeypatch.setattr(ct, "_MIN_CHUNK", 256)        # force several doublings
    monkeypatch.setattr(ct, "_BYTES_PER_ROW", 1)
    p, full = csv_file
    got = read_tail(p, rows=900)
    pd.testing.assert_frame_equal(got.tail(900).reset_index(drop=True),
                                  full.tail(900).reset_index(drop=True), check_dtype=False)


def test_since_ms_covers_the_window(csv_file, monkeypatch):
    import shared.csvtail as ct
    monkeypatch.setattr(ct, "_MIN_CHUNK", 512)
    p, full = csv_file
    cutoff = int(full["timestamp"].iloc[-400])
    got = read_tail(p, since_ms=cutoff)
    want = full[full["timestamp"] >= cutoff].reset_index(drop=True)
    pd.testing.assert_frame_equal(got[got["timestamp"] >= cutoff].reset_index(drop=True),
                                  want, check_dtype=False)
    assert len(want) == 400


def test_columns_subset(csv_file):
    p, full = csv_file
    got = read_tail(p, rows=50, columns=["close"])
    assert list(got.columns) == ["close"]
    assert got["close"].tail(50).tolist() == full["close"].tail(50).tolist()


def test_last_ts(csv_file):
    p, full = csv_file
    assert last_ts_ms(p) == int(full["timestamp"].iloc[-1])


def test_torn_final_line_is_dropped(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("timestamp,open,high,low,close,volume\n"
                 "1000,1,2,0.5,1.5,10\n2000,1.5,2.5,1,2,11\n3000,2,3", encoding="utf-8")
    assert last_ts_ms(p) == 2000
    assert read_tail(p, rows=5)["timestamp"].tolist() == [1000, 2000]


def test_complete_final_line_without_newline_is_kept(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("timestamp,open,high,low,close,volume\n"
                 "1000,1,2,0.5,1.5,10\n2000,1.5,2.5,1,2,11", encoding="utf-8")
    assert last_ts_ms(p) == 2000
    assert read_tail(p, rows=5)["timestamp"].tolist() == [1000, 2000]


def test_crlf_files(tmp_path):
    # okx_fetch appends with csv.writer, whose line terminator is \r\n
    p = tmp_path / "t.csv"
    p.write_bytes(b"timestamp,open,high,low,close,volume\r\n"
                  b"1000,1,2,0.5,1.5,10\r\n2000,1.5,2.5,1,2,11\r\n")
    assert last_ts_ms(p) == 2000
    df = read_tail(p, rows=1)
    assert df["timestamp"].tolist()[-1] == 2000 and df["close"].tolist()[-1] == 2.0


def test_header_only_and_missing(tmp_path):
    p = tmp_path / "h.csv"
    p.write_text(",".join(CANDLE_COLUMNS) + "\n", encoding="utf-8")
    assert last_ts_ms(p) is None
    assert len(read_tail(p, rows=10)) == 0
    assert last_ts_ms(tmp_path / "nope.csv") is None
    (tmp_path / "empty.csv").write_text("", encoding="utf-8")
    assert last_ts_ms(tmp_path / "empty.csv") is None
