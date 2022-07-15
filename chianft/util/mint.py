import argparse
import asyncio
import csv
from functools import wraps
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union, Set

from blspy import AugSchemeMPL, G1Element, G2Element, PrivateKey
from clvm.casts import int_from_bytes, int_to_bytes

from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.rpc.full_node_rpc_client import FullNodeRpcClient
from chia.rpc.rpc_client import RpcClient
from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint16, uint32, uint64
from chia.types.spend_bundle import SpendBundle
from chia.util.bech32m import decode_puzzle_hash, encode_puzzle_hash
from chia.util.byte_types import hexstr_to_bytes
from chia.util.condition_tools import ConditionOpcode
from chia.util.config import load_config
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia.util.ints import uint16, uint64
from chia.wallet.puzzles.load_clvm import load_clvm
from chia.wallet.util.wallet_types import WalletType

import warnings
warnings.filterwarnings("ignore", category=UserWarning)


config = load_config(Path(DEFAULT_ROOT_PATH), "config.yaml")
testnet_agg_sig_data = config["network_overrides"]["constants"]["testnet10"]["AGG_SIG_ME_ADDITIONAL_DATA"]
DEFAULT_CONSTANTS = DEFAULT_CONSTANTS.replace_str_to_bytes(**{"AGG_SIG_ME_ADDITIONAL_DATA": testnet_agg_sig_data})


class Minter:
    def __init__(
        self,
        wallet_client: Optional[WalletRpcClient] = None,
        node_client: Optional[FullNodeRpcClient] = None,
    ) -> None:
        self.wallet_client = wallet_client
        self.node_client = node_client

    async def connect(self, fingerprint: Optional[int] = None) -> None:
        config = load_config(Path(DEFAULT_ROOT_PATH), "config.yaml")
        rpc_host = config["self_hostname"]
        full_node_rpc_port = config["full_node"]["rpc_port"]
        wallet_rpc_port = config["wallet"]["rpc_port"]
        if not self.node_client:
            self.node_client = await FullNodeRpcClient.create(
                rpc_host, uint16(full_node_rpc_port), Path(DEFAULT_ROOT_PATH), config
            )
        if not self.wallet_client:
            self.wallet_client = await WalletRpcClient.create(
                rpc_host, uint16(wallet_rpc_port), Path(DEFAULT_ROOT_PATH), config
            )
        if fingerprint:
            await self.wallet_client.log_in(fingerprint)
        xch_wallets = await self.wallet_client.get_wallets(wallet_type=WalletType.STANDARD_WALLET)
        did_wallets = await self.wallet_client.get_wallets(wallet_type=WalletType.DECENTRALIZED_ID)
        self.xch_wallet_id = xch_wallets[0]["id"]
        self.did_wallet_id = did_wallets[0]["id"]

    async def close(self) -> None:
        if self.node_client:
            self.node_client.close()

        if self.wallet_client:
            self.wallet_client.close()

    async def get_funding_coin(self, amount: int) -> Dict:
        coins = await self.wallet_client.select_coins(amount=amount, wallet_id=self.xch_wallet_id)
        if len(coins) > 1:
            raise ValueError("Bulk minting requires a single coin with value greater than %s" % amount)
        return coins[0]

    async def get_did_coin(self) -> Dict:
        coins = await self.wallet_client.select_coins(amount=1, wallet_id=self.did_wallet_id)
        return coins[0]
    
    async def get_mempool_cost(self) -> uint64:
        mempool_items = await self.node_client.get_all_mempool_items()
        cost = 0
        for item in mempool_items.values():
            cost += item["cost"]
        return cost

    async def get_tx_from_mempool(self, sb_name: bytes32) -> Tuple[bool, Optional[bytes32]]:
        mempool_items = await self.node_client.get_all_mempool_items()
        for item in mempool_items.items():
            if bytes32(hexstr_to_bytes(item[1]["spend_bundle_name"])) == sb_name:
                return True, item[0]
        return False, None

    async def wait_tx_confirmed(self, tx_id: bytes32) -> bool:
        while True:
            item = await self.node_client.get_mempool_item_by_tx_id(tx_id)
            mempool_items = await self.node_client.get_all_mempool_items()
            if item is None:
                return True
            else:
                await asyncio.sleep(1)

    async def create_spend_bundles(
        self,
        metadata_input: Path,
        bundle_output: Path,
        wallet_id: int,
        royalty_address: Optional[str] = None,
        royalty_percentage: Optional[int] = None,
        has_targets: Optional[bool] = True,
    ) -> None:
        metadata_list, target_list = read_metadata_csv(metadata_input, has_header=True, has_targets=has_targets)
        n = len(metadata_list)
        chunk = 25
        funding_coin = await self.get_funding_coin(n)
        did_coin = await self.get_did_coin()
        did_lineage_parent = None
        next_coin = funding_coin
        spend_bundles = []
        for i in range(0, n, chunk):
            resp = await self.wallet_client.did_mint_nfts(
                wallet_id=self.did_wallet_id,
                metadata_list=metadata_list[i:i+chunk],
                target_list=target_list[i:i+chunk],
                royalty_percentage=royalty_percentage,
                royalty_address=royalty_address,
                starting_num=i+1,
                max_num=n,
                xch_coins=next_coin.to_json_dict(),
                xch_change_ph=next_coin.to_json_dict()["puzzle_hash"],
                did_coin=did_coin.to_json_dict(),
                did_lineage_parent=did_lineage_parent,
            )
            if not resp["success"]:
                raise ValueError("SpendBundle was not able to be created for metadata rows: %s to %s" % (i, i+chunk))
            sb = SpendBundle.from_json_dict(resp["spend_bundle"])
            spend_bundles.append(bytes(sb))
            next_coin = [c for c in sb.additions() if c.puzzle_hash == funding_coin.puzzle_hash][0]
            did_lineage_parent = [c for c in sb.removals() if c.name() == did_coin.name()][0].parent_coin_info.hex()
            did_coin = [c for c in sb.additions() if (c.parent_coin_info == did_coin.name()) and (c.amount == 1)][0]
        return spend_bundles

    async def submit_spend_bundles(self, spend_bundles: List[SpendBundle]) -> None:
        MAX_COST = 11000000000
        for i, sb in enumerate(spend_bundles):
            sb_cost = 0
            for spend in sb.coin_spends:
                cost, _ = spend.puzzle_reveal.to_program().run_with_cost(MAX_COST, spend.solution.to_program())
                sb_cost += cost

            print("Submitting SB with cost: %s" % sb_cost)
            resp = await self.node_client.push_tx(sb)
            assert resp["success"]
            print("SB successfully added to mempool")
            while True:
                in_mempool, tx_id = await self.get_tx_from_mempool(sb.name())
                if in_mempool:
                    break

            await self.wait_tx_confirmed(tx_id)
            await asyncio.sleep(2)


def read_metadata_csv(
    file_path: Path,
    has_header: Optional[bool] = False,
    has_targets: Optional[bool] = False,
) -> List[Dict]:
    with open(file_path, "r") as f:
        csv_reader = csv.reader(f)
        bulk_data = list(csv_reader)
    metadata_list = []
    if has_header:
        header_row = bulk_data[0]
        rows = bulk_data[1:]
    else:
        header_row = ["hash", "uris", "meta_hash", "meta_uris", "license_hash", "license_uris", "series_number", "series_total"]
        if has_targets:
            header_row.append["target"]
        rows = bulk_data
    list_headers = ["uris", "meta_uris", "license_uris"]
    targets = []
    for row in rows:
        meta_dict = {list_headers[i]: [] for i in range(len(list_headers))}
        for i, header in enumerate(header_row):
            if header in list_headers:
                meta_dict[header].append(row[i])
            elif header == "target":
                targets.append(row[i])
            else:
                meta_dict[header] = row[i]
        metadata_list.append(meta_dict)
    return metadata_list, targets
        
