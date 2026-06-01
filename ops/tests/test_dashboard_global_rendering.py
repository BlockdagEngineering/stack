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

    def test_global_table_uses_chain_blocks_and_hides_removed_columns(self) -> None:
        section = self.global_section()

        self.assertNotIn('<th class="right">Shares In Window</th>', section)
        self.assertNotIn('<th class="nowrap">Nodes</th>', section)
        self.assertIn('<table class="wide-table equal-column-table">', section)
        self.assertIn(".equal-column-table", dashboard.HTML)
        self.assertIn("table-layout: fixed;", dashboard.HTML)
        self.assertIn('<th class="right">Chain Blocks In Window</th>', section)
        self.assertLess(
            section.index('<th class="right">Chain Blocks In Window</th>'),
            section.index('<th class="right">Work %</th>'),
        )
        self.assertNotIn('<th class="right">Avg USD/h</th>', section)
        self.assertNotIn('<th class="right">Wallet Avg BDAG/h</th>', section)
        self.assertNotIn('<th class="right">Credit Blocks</th>', section)
        self.assertNotIn('<th class="right">Found Blocks</th>', section)
        self.assertIn('id="globalTableWindow"', section)
        self.assertIn("Table period: waiting for scan window.", section)
        self.assertIn(
            "Pool rows use chain-confirmed production over the displayed Scan Window.",
            section,
        )
        self.assertIn("Credit-block and found-block duplicates are intentionally hidden", section)

    def test_global_rows_render_chain_blocks_without_removed_columns(self) -> None:
        html = dashboard.HTML

        self.assertIn("function formatGlobalTableWindow(data)", html)
        self.assertIn('text("globalTableWindow", formatGlobalTableWindow(data));', html)
        self.assertIn("const chainBlocks = firstPresent(row.blocks, row.found_blocks);", html)
        self.assertNotIn("const shares = firstPresent(row.shares, row.blocks);", html)
        self.assertNotIn("const avgUsd = firstPresent(row.estimated_usd_avg_hour, row.estimated_usd_recent_hour);", html)
        self.assertNotIn("const avgBdag = firstPresent(row.estimated_bdag_avg_hour, row.estimated_bdag_recent_hour);", html)
        self.assertNotIn("const nodes = globalNodesLabel(row);", html)
        self.assertNotIn("const shares = row.blocks;", html)
        self.assertIn('colspan="8"', html)
        self.assertNotIn('colspan="9"', html)

    def test_miners_table_filters_stale_inactive_inventory_rows(self) -> None:
        html = dashboard.HTML

        self.assertIn("Active Miner Lanes", html)
        self.assertIn("function activeMinerLaneRow(miner)", html)
        self.assertIn("const rows = allRows.filter(activeMinerLaneRow);", html)
        self.assertIn("hidden-inactive=", html)
        self.assertIn("No active miner lanes are currently present.", html)
        self.assertNotIn("Tracked Miner Health", html)

    def test_plot_refresh_and_sampler_defaults_are_one_minute(self) -> None:
        html = dashboard.HTML

        self.assertEqual(dashboard.EARNINGS_SAMPLER_INTERVAL_SECONDS, 60.0)
        self.assertEqual(dashboard.GLOBAL_SAMPLER_INTERVAL_SECONDS, 60.0)
        self.assertIn("setInterval(refresh, 60000);", html)
        self.assertIn(")) refreshEarnings();\n    }, 60000);", html)
        self.assertIn("refreshGlobal(); }, 60000);", html)
        self.assertIn("let earningsRefreshInFlight = false;", html)
        self.assertIn("let globalRefreshInFlight = false;", html)
        self.assertNotIn("refreshGlobal(); }, 300000);", html)


if __name__ == "__main__":
    unittest.main()
