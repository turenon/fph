from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from fph.cli import main


class CliTest(unittest.TestCase):
    def test_init_stats_bargains_export_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.toml"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(["init", "-o", str(cfg_path)])
            self.assertEqual(rc, 0)
            self.assertTrue(cfg_path.exists())

            # Update db_path inside generated config to stay inside tmpdir
            content = cfg_path.read_text(encoding="utf-8").replace(
                'db_path = "data/fph.db"',
                f'db_path = "{(Path(tmpdir) / "fph.db").as_posix()}"',
            )
            cfg_path.write_text(content, encoding="utf-8")

            out_stats = io.StringIO()
            with redirect_stdout(out_stats):
                self.assertEqual(main(["-c", str(cfg_path), "stats"]), 0)
            self.assertIn("暂无数据", out_stats.getvalue())

            out_bargains = io.StringIO()
            with redirect_stdout(out_bargains):
                self.assertEqual(main(["-c", str(cfg_path), "bargains", "--city", "福州"]), 0)
            self.assertIn("暂无数据", out_bargains.getvalue())

            export_file = Path(tmpdir) / "out.csv"
            out_exp = io.StringIO()
            with redirect_stdout(out_exp):
                self.assertEqual(
                    main(["-c", str(cfg_path), "export", "--dataset", "all", "-o", str(export_file)]),
                    0,
                )
            self.assertTrue(export_file.exists())


if __name__ == "__main__":
    unittest.main()
