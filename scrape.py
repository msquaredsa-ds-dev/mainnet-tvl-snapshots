from email.policy import default
import pandas as pd
import string
import git
import hashlib
import json
from pathlib import Path
import argparse
import datetime
import traceback
import requests
from collections import defaultdict
import time
import datetime
import numpy as np


def iterate_file_versions(
    repo_path, filepath, ref="main", commits_to_skip=None, show_progress=False
):
    relative_path = str(Path(filepath).relative_to(repo_path))
    repo = git.Repo(repo_path, odbt=git.GitDB)
    commits = reversed(list(repo.iter_commits(ref, paths=[relative_path])))
    progress_bar = None
    if commits_to_skip:
        # Filter down to just the ones we haven't seen
        new_commits = [
            commit for commit in commits if commit.hexsha not in commits_to_skip
        ]
        commits = new_commits
    if show_progress:
        progress_bar = click.progressbar(commits, show_pos=True, show_percent=True)
    for commit in commits:
        if progress_bar:
            progress_bar.update(1)
        try:
            blob = [b for b in commit.tree.blobs if b.name == relative_path][0]
            yield commit.committed_datetime, commit.hexsha, blob.data_stream.read()
        except IndexError:
            # This commit doesn't have a copy of the requested file
            pass


# MAPPING = {
#     "sol": ["solana", "SOL", "sol"], 
#     # "msol": ["msol", "mSOL", "marinade"],
#     # "btc": ["bitcoin", "BTC", "btc"],
#     # "sbr": ["saber", "SBR", "sbr"],
#     # "ftt": ["ftx-token", "FTT", "ftt"], 
#     # "usdc": ["usd-coin", "USDC", "usdc"],
#     # "luna": ["terra-luna", "LUNA", "luna"],
#     # "scn": ["socean-staked-sol", "scnSOL", "socean"],
#     # "srm": ["serum", "SRM", "serum"],
#     # "mngo": ["mango-markets", "MNGO", "mngo"],
#     # "ray": ["raydium", "RAY", "ray"],
#     # "eth": ["ethereum", "ETH", "eth"]
# }

DESIRED_COLS = [
    "coinsByCoingeckoId", 
    "pricesByCoingeckoId", 
    "sharePricesByGlobalId", 
    "depositTokenByGlobalId",
    "usdValueByGlobalId"
]

def populate_registry():
    try:
        return dict(json.loads(requests.get("https://app.friktion.fi/mainnet-registry.json").content))
    except Exception as e:
        traceback.print_exc()
        with open('registry.json', 'r') as f:
            return dict(json.load(f))


MAINNET_REGISTRY = populate_registry()
EXCLUDE_COMMITS = [
    "218c73176ca660b8592695d055d5db669fd413da", 
    "4c57939521ea45765becdae22fc809e4491dfb4a",
    "a21d5be807b7444c79744597202baabe55624cfd",
    "40f62eeb528ae099b14a30c8dbdb2b0770b92fe3",
]


def process_diff(diff, info):
    assert len(diff) == 3
    utc_time = int(datetime.datetime.timestamp(diff[0])*1000)
    commit_hash = diff[1]
    if commit_hash in EXCLUDE_COMMITS:
        return
    try:
        content = json.loads(diff[2])
    except Exception as e:
        print(commit_hash + " is ill formatted. Skipped...")
        return

    for metadata in info:
        # Skip incomplete history
        if "sharePricesByGlobalId" not in content.keys():
            return

        row = [utc_time]
        # Populate coin balances
        globalId = metadata["globalId"]
        coingeckoId = metadata["depositTokenCoingeckoId"]
        symbolId = metadata["depositTokenSymbol"]
        output = metadata["output"]
        
        if DESIRED_COLS[0] in content and coingeckoId in content[DESIRED_COLS[0]]:
            row.append(content[DESIRED_COLS[0]][coingeckoId])
        else:
            row.append("")

        if DESIRED_COLS[1] in content and symbolId in content[DESIRED_COLS[1]]:
            row.append(content[DESIRED_COLS[1]][symbolId])
        else:
            row.append("")

        for col in DESIRED_COLS[2:]:
            if col in content and globalId in content[col]:
                row.append(content[col][globalId])
                # if content[col][globalId]==1.05422:
                #     print(commit_hash)
    
        if len(row) >= len(DESIRED_COLS)+1:
            output.append(row)


def parse_tvls(diff, tvls,):
    utc_time = int(datetime.datetime.timestamp(diff[0])*1000)
    row = [utc_time]
    try:
        content = json.loads(diff[2])
        if "totalTvlUSD" in content:
            row.append(content["totalTvlUSD"])
        tvls.append(row)
    except Exception as e:
        print("     error parsing TVLs")
        return


def accum(diff, birdy_tvls):
    utc_time = int(datetime.datetime.timestamp(diff[0])*1000)
    row = {"timestamp": utc_time}
    try:
        content = json.loads(diff[2])
        if "usdValueByGlobalId" in content:
            row.update(content["usdValueByGlobalId"])
            birdy_tvls.append(row)
    except Exception as e:
        print(" is ill formatted. Skipped...")
        return


def parse_spot(diff, spots):
    utc_time = int(datetime.datetime.timestamp(diff[0])*1000)
    try:
        content = json.loads(diff[2])
        if "pricesByCoingeckoId" in content:
            for symbol, price in content["pricesByCoingeckoId"].items():
                if symbol in spots.keys():
                    spots[symbol].append([utc_time, price])
                else:
                    spots[symbol] = [[utc_time, price]]
    except Exception as e:
        traceback.print_exc()
        print(" is ill formatted. Skipped...")
        return


def get_coingecko_mapping():
    mapping = {}
    for _, payload in MAINNET_REGISTRY.items():
        if "volt01" in payload:
            payload2 = payload["volt01"]
            mapping[payload2["underlyingTokenSymbol"]] = payload2["underlyingTokenCoingeckoId"]

    return mapping


def parse_args():
    parser = argparse.ArgumentParser(description='Parse Args')
    parser.add_argument('--input_file', type=str, default="friktionSnapshot.json")
    parser.add_argument('--output_file', type=str)
    parser.add_argument('--path_to_repo', type=str, default="./")
    # parser.add_argument('--append', type=str, default=True)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    diffs = iterate_file_versions(args.path_to_repo, args.input_file)

    info = []
    tvls = []
    spot = {}
    tvls_birdy = []
    # Grab metadata for all vaults
    for globalId, payload in MAINNET_REGISTRY.items():
        for col in DESIRED_COLS:
            datastream_file = Path('derived_timeseries/{}_{}.json'.format(globalId, col))
            datastream_file.touch(exist_ok=True)
        print(globalId)

        # Brittle hardcode to find the voltType
        payload_key = None
        if type(payload) != dict:
            continue
        for key in payload.keys():
            if "volt" in key and type(payload[key])==dict:
                payload_key = key
                break        
        assert payload_key, "no valid volt type in payload. Contact dai"

        metadata = {
            "globalId": globalId,
            "depositTokenCoingeckoId": payload[payload_key]["depositTokenCoingeckoId"], 
            "depositTokenSymbol": payload[payload_key]["depositTokenSymbol"],
            # Use this to store the output from diff processing
            "output": []
        }
        info.append(metadata)

    # Get symbol -> CoingeckoId mapping 
    mapping = get_coingecko_mapping()

    for diff in diffs:
        process_diff(diff, info)
        parse_tvls(diff, tvls)
        accum(diff, tvls_birdy)
        parse_spot(diff, spot)

    df_tvl = pd.DataFrame(tvls_birdy)
    df_tvl["timestamp"] = pd.to_datetime(df_tvl.timestamp, unit='ms')
    df_tvl = df_tvl.set_index("timestamp")
    df_tvl = df_tvl.groupby(df_tvl.index.floor('H')).first()
    df_tvl.index = (df_tvl.index.astype('int')/10**6).astype('int')
    columns = list(df_tvl.columns)
    # Some weird NaN -> None conversion going on in this comprehension b/c python is gay as fuck
    values = [[idx, [row[col] if not np.isnan(row[col]) else None for col in columns]] for idx, row in df_tvl.iterrows()]
    tvl_final = []
    tvl_final.append(columns)
    tvl_final += values
    
    # Write TVLs for Birdy
    tvl_filename = Path('derived_timeseries/tvl_usd_agg.json')
    tvl_filename.touch(exist_ok=True)
    with open('derived_timeseries/tvl_usd_agg.json', 'w') as fl:
        json.dump(tvl_final, fl, separators=(',', ':'), indent=2)    

    # Write TVLs
    tvl_filename = Path('derived_timeseries/tvl.json')
    tvl_filename.touch(exist_ok=True)
    with open('derived_timeseries/tvl.json', 'w') as fl:
        json.dump(tvls, fl, separators=(',', ':'), indent=2)    

    for symbol in spot.keys():
        spot_filename = Path('derived_timeseries/spot.json')
        spot_filename.touch(exist_ok=True)
        coingeckoId = mapping[symbol] if symbol in mapping else 0
        if not coingeckoId:
            continue
        print(coingeckoId)
        with open('derived_timeseries/{}_pricesByCoingeckoId.json'.format(coingeckoId), 'w') as fl:
            json.dump(spot[symbol], fl, separators=(',', ':'), indent=2)    


    for metadata in info:
        for idx, col in enumerate(DESIRED_COLS):
            fname = "derived_timeseries/{}_{}.json".format(metadata["globalId"], col)
            output = metadata["output"]
            with open(fname, 'r') as openfile:
                try:
                    writedata = json.load(openfile)
                except:
                    writedata = []

            # Skip captured commits
            cached_rows = [x[0] for x in writedata if len(x)]
            last_timestamp = writedata[-1][0] if len(writedata) else 0

            filtered_rows = list(filter(lambda x: x[0] not in cached_rows and x[0] > last_timestamp, output))
            filtered_data = [[x[0], x[idx+1]] for x in filtered_rows]
            if not len(filtered_data):
                continue
            # Remove row if it's the same value as the one before
            duplicates_removed = []
            for data_idx, data in enumerate(filtered_data):
                if data_idx == 0 or filtered_data[data_idx-1][1] == data[1]:
                    continue
                else:
                    duplicates_removed.append(data)
            print(len(duplicates_removed))
            writedata.extend(duplicates_removed)
            # print(writedata)
            with open(fname, 'w') as fl:
                json.dump(writedata, fl, separators=(',', ':'), indent=2)
