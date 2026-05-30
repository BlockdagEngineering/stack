#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import dashboard  # noqa: E402


class DashboardGlobalRenderingTests(unittest.TestCase):
    def global_section(self) -> str:
        html = dashboard.HTML
        start = html.index('<section id="tab-global"')
        end = html.index('<section id="tab-earnings"', start)
        return html[start:end]

    def test_global_table_keeps_local_shares_and_hides_duplicate_block_columns(self) -> None:
        section = self.global_section()

        self.assertIn('<th class="right">Shares In Window</th>', section)
        self.assertIn('<th class="right">Chain Blocks In Window</th>', section)
        self.assertNotIn('<th class="right">Credit Blocks</th>', section)
        self.assertNotIn('<th class="right">Found Blocks</th>', section)
        self.assertIn('id="globalTableWindow"', section)
        self.assertIn("Table period: waiting for scan window.", section)
        self.assertIn(
            "Shares use the local pool credit count over the displayed Scan Window when available",
            section,
        )
        self.assertIn("Credit-block and found-block duplicates are intentionally hidden", section)

    def test_global_rows_render_shares_before_chain_block_fallback(self) -> None:
        html = dashboard.HTML

        self.assertIn("function formatGlobalTableWindow(data)", html)
        self.assertIn('text("globalTableWindow", formatGlobalTableWindow(data));', html)
        self.assertIn("const shares = firstPresent(row.shares, row.blocks);", html)
        self.assertIn("const chainBlocks = firstPresent(row.blocks, row.found_blocks);", html)
        self.assertNotIn("const shares = row.blocks;", html)
        self.assertIn('colspan="12"', html)
        self.assertNotIn('colspan="13"', html)


if __name__ == "__main__":
    unittest.main()
