import copy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from backend.universe import AS_OF, KRX_URL, MAX_BYTES, RULES_URL, import_universe, load_universe, main


def snapshot():
    # 검증 경로를 시험하는 합성 목록. 실제 대회 종목으로 저장하지 않는다.
    rows = [{"symbol": f"{index + 1:06}", "name": f"시험종목{index + 1}",
             "board": "KOSPI" if index < 200 else "KOSDAQ"} for index in range(350)]
    sources = [{"board": board, "index_name": name, "as_of": AS_OF,
                "row_count": count, "filename": filename, "sha256": "a" * 64, "source_url": KRX_URL}
               for board, name, count, filename in [("KOSPI", "코스피 200", 200, "kospi.csv"),
                                                     ("KOSDAQ", "코스닥 150", 150, "kosdaq.csv")]]
    return {"status": "verified", "as_of": AS_OF,
            "checked_at": "2026-10-04T12:00:00+09:00", "source_url": KRX_URL,
            "source_kind": "official_index_constituents", "count": len(rows), "rows": rows,
            "sources": sources}


class UniverseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "universe.json"

    def load(self, data):
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return load_universe(self.path)

    def assert_unverified(self, result):
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["as_of"], AS_OF)
        self.assertTrue(result["error"])

    def test_missing_file_has_no_fallback_symbols(self):
        self.assert_unverified(load_universe(self.path))

    def test_complete_fixed_date_index_snapshot_is_preserved(self):
        source = snapshot()
        actual = self.load(source)
        self.assertEqual(actual["status"], "verified")
        self.assertEqual(actual["rows"], source["rows"])
        self.assertEqual(actual["count"], 350)
        self.assertEqual(actual["checked_at"], source["checked_at"])
        self.assertEqual(actual["source_url"], KRX_URL)
        self.assertEqual(actual["sources"], source["sources"])
        self.assertIn("당일 거래 제한", actual["provenance"])

    def test_actual_index_count_and_alphanumeric_symbols_are_preserved(self):
        source = snapshot()
        source["rows"][0].update(symbol="0126Z0", name="삼성에피스홀딩스")
        source["rows"].append({"symbol": "0220W0", "name": "한화머시너리앤서비스홀딩스", "board": "KOSPI"})
        source["count"] = 351
        source["sources"][0]["row_count"] = 201
        actual = self.load(source)
        self.assertEqual(actual["status"], "verified")
        self.assertEqual(actual["count"], 351)
        self.assertEqual(actual["rows"], source["rows"])

    def test_index_sources_require_two_distinct_verified_files(self):
        source = snapshot()
        for sources in (None, [], source["sources"][:1], [source["sources"][0]] * 2):
            source = snapshot()
            source["sources"] = sources
            self.assert_unverified(self.load(source))
        for patch in ({"row_count": True}, {"row_count": 201}, {"as_of": "2026-10-01"},
                      {"index_name": "코스피"}, {"sha256": "bad"}, {"filename": "../secret.csv"},
                      {"source_url": "https://example.com/list"}):
            source = snapshot()
            source["sources"][0].update(patch)
            self.assert_unverified(self.load(source))

    def test_official_competition_list_may_exclude_index_constituents(self):
        source = snapshot()
        source.update(source_url=RULES_URL, source_kind="official_competition_list")
        source["rows"].pop()
        source["count"] -= 1
        self.assertEqual(self.load(source)["status"], "verified")

    def test_incomplete_or_wrong_board_index_list_is_rejected(self):
        source = snapshot()
        source["rows"].pop()
        source["count"] -= 1
        self.assert_unverified(self.load(source))
        source = snapshot()
        source["rows"][0]["board"] = "KOSDAQ"
        self.assert_unverified(self.load(source))

    def test_invalid_metadata_cannot_publish_partial_rows(self):
        cases = {"status": ["unverified", None], "as_of": ["2026-10-02", None],
                 "checked_at": [None, "not-a-date", "2026-10-04T12:00:00", "2026-09-20T12:00:00+09:00"],
                 "source_kind": [None, [], "current_master"],
                 "source_url": [None, "http://data.krx.co.kr/x", "https://data.krx.co.kr.evil.example/x",
                                "https://secret@data.krx.co.kr/x", "https://data.krx.co.kr:444/x"],
                 "count": [True, 349, 351]}
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    source = snapshot()
                    source[field] = value
                    self.assert_unverified(self.load(source))

    def test_duplicate_or_malformed_rows_are_rejected(self):
        cases = [{"symbol": "1"}, {"symbol": 1}, {"symbol": "000002"}, {"symbol": "0126z0"},
                 {"name": " "}, {"name": "bad\nname"}, {"board": "KONEX"}, {"board": []}]
        for patch in cases:
            with self.subTest(patch=patch):
                source = snapshot()
                source["rows"][0].update(copy.deepcopy(patch))
                self.assert_unverified(self.load(source))

    def test_corrupt_and_oversized_files_fail_closed(self):
        for content in [b"{broken", b"[]", b"\xff", b" " * (MAX_BYTES + 1)]:
            self.path.write_bytes(content)
            self.assert_unverified(load_universe(self.path))

    def test_import_checks_then_persists_without_temporary_file(self):
        self.load(snapshot())
        target = self.path.parent / "config" / "competition-universe.json"
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["import", str(self.path), "--output", str(target)]), 0)
        self.assertIn("350", output.getvalue())
        self.assertEqual(load_universe(target)["rows"], snapshot()["rows"])
        self.assertEqual(list(target.parent.iterdir()), [target])

    def test_failed_import_preserves_existing_snapshot(self):
        target = self.path.parent / "published.json"
        target.write_text("existing data", encoding="utf-8")
        self.load({"rows": [{"symbol": "005930"}]})
        with self.assertRaises(ValueError):
            import_universe(self.path, target)
        self.assertEqual(target.read_text(encoding="utf-8"), "existing data")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["import", str(self.path), "--output", str(target)]), 2)


if __name__ == "__main__":
    unittest.main()
