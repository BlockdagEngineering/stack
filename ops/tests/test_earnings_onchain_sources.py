#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class EarningsOnchainSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            name: getattr(pool_ops, name)
            for name in (
                "archive_rpc_urls",
                "blockscout_v2_address_transactions",
                "ensure_runtime",
                "first_block_at_or_after",
                "global_evm_rpc_urls",
                "json_rpc_balance_at",
                "json_rpc_call",
                "node_rpc_urls",
                "read_json_file",
                "rpc_block_timestamp",
                "seconds_since_epoch",
                "write_json_file",
            )
        }
        self.addCleanup(self.restore)

    def restore(self) -> None:
        for name, value in self.originals.items():
            setattr(pool_ops, name, value)

    def test_wallet_window_uses_evm_rpc_not_authenticated_mining_rpc(self) -> None:
        calls = []
        address = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.ensure_runtime = lambda: None
        pool_ops.seconds_since_epoch = lambda: 1_000_000
        pool_ops.read_json_file = lambda _path, default: {}
        pool_ops.write_json_file = lambda _path, _payload, mode=0o600: None
        pool_ops.global_evm_rpc_urls = lambda: [("local-evm", "http://evm-rpc")]
        pool_ops.node_rpc_urls = lambda: [("mining-rpc", "http://mining-rpc")]
        pool_ops.archive_rpc_urls = lambda: [("archive-evm", "http://archive-rpc")]
        pool_ops.first_block_at_or_after = lambda url, latest, target: 90
        pool_ops.rpc_block_timestamp = lambda url, block: 100_000 if block == 100 else 96_400
        pool_ops.blockscout_v2_address_transactions = lambda _address, _cutoff_at: {
            "source": "blockscout",
            "items": [],
        }

        def fake_json_rpc_call(url, method, params, timeout=6.0):
            calls.append((url, method))
            self.assertEqual(url, "http://evm-rpc")
            self.assertEqual(method, "eth_blockNumber")
            return "0x64"

        def fake_balance_at(url, _address, block_number, timeout=8.0):
            calls.append((url, f"balance:{block_number}"))
            self.assertNotEqual(url, "http://mining-rpc")
            if block_number == 100:
                self.assertEqual(url, "http://evm-rpc")
                return {"wei": str(200 * pool_ops.WEI_PER_BDAG), "bdag": "200.00"}
            self.assertEqual(url, "http://archive-rpc")
            return {"wei": str(150 * pool_ops.WEI_PER_BDAG), "bdag": "150.00"}

        pool_ops.json_rpc_call = fake_json_rpc_call
        pool_ops.json_rpc_balance_at = fake_balance_at

        result = pool_ops.collect_onchain_wallet_window_earnings(address, hours=1)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source"], "on-chain-balance-reconciled-with-native-transfers")
        self.assertEqual(result["latest_balance_source"], "local-evm")
        self.assertEqual(result["start_balance_source"], "archive-evm")
        self.assertEqual(result["earned_bdag"], "50.00")
        self.assertNotIn(("http://mining-rpc", "eth_blockNumber"), calls)

    def test_earnings_24h_never_relabels_db_credits_as_source_truth(self) -> None:
        old_collect_onchain = pool_ops.collect_onchain_wallet_window_earnings
        old_collect_credit_totals = pool_ops.collect_credit_totals
        old_fetch_cmc_price = pool_ops.fetch_cmc_price
        old_read_env_value = pool_ops.read_env_value
        old_collect_wallet_balances = pool_ops.collect_wallet_balances
        old_collect_miner_earnings_estimates = pool_ops.collect_miner_earnings_estimates
        old_collect_wallet_balances_for_addresses = pool_ops.collect_wallet_balances_for_addresses
        old_read_history = pool_ops.read_compact_earnings_history_for_dashboard
        self.addCleanup(lambda: setattr(pool_ops, "collect_onchain_wallet_window_earnings", old_collect_onchain))
        self.addCleanup(lambda: setattr(pool_ops, "collect_credit_totals", old_collect_credit_totals))
        self.addCleanup(lambda: setattr(pool_ops, "fetch_cmc_price", old_fetch_cmc_price))
        self.addCleanup(lambda: setattr(pool_ops, "read_env_value", old_read_env_value))
        self.addCleanup(lambda: setattr(pool_ops, "collect_wallet_balances", old_collect_wallet_balances))
        self.addCleanup(lambda: setattr(pool_ops, "collect_miner_earnings_estimates", old_collect_miner_earnings_estimates))
        self.addCleanup(lambda: setattr(pool_ops, "collect_wallet_balances_for_addresses", old_collect_wallet_balances_for_addresses))
        self.addCleanup(lambda: setattr(pool_ops, "read_compact_earnings_history_for_dashboard", old_read_history))

        pool_ops.read_env_value = lambda _name: "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.collect_credit_totals = lambda: {
            "totals": {"total_wei": str(1000 * pool_ops.WEI_PER_BDAG), "total_bdag": "1000.00", "first_credit_at": None},
            "recent_1h": {"total_wei": str(10 * pool_ops.WEI_PER_BDAG), "total_bdag": "10.00"},
            "recent_24h": {
                "wallet_total_wei": str(240 * pool_ops.WEI_PER_BDAG),
                "total_wei": str(240 * pool_ops.WEI_PER_BDAG),
                "credit_count": 1,
            },
            "recent_24h_by_address": [],
            "by_address": [],
        }
        pool_ops.fetch_cmc_price = lambda: {"status": "ok", "usd": "1", "zar": "20"}
        pool_ops.collect_wallet_balances = lambda _address: {"sources": []}
        pool_ops.collect_wallet_balances_for_addresses = lambda _addresses: {
            "status": "ok",
            "source_truth": "on-chain eth_getBalance latest",
            "address_count": 1,
            "ok_address_count": 1,
            "total_bdag": "1000.00",
            "addresses": [],
        }
        pool_ops.collect_miner_earnings_estimates = lambda _credits, _price: []
        pool_ops.read_compact_earnings_history_for_dashboard = lambda: ([], 0)
        pool_ops.collect_onchain_wallet_window_earnings = lambda _address, hours=24: {
            "status": "failed",
            "hours": hours,
            "error": "simulated chain source outage",
        }

        result = pool_ops.collect_earnings(include_history=False)

        self.assertEqual(result["earnings_24h"]["source"], "on-chain-unavailable")
        self.assertIsNone(result["earnings_24h"]["bdag"])
        self.assertFalse(result["earnings_24h"]["fallback_used"])
        self.assertEqual(result["earnings_24h"]["db_credit_diagnostic_bdag"], "240.00")


if __name__ == "__main__":
    unittest.main()
