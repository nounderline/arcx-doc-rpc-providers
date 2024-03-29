import os
import h2.exceptions
import asyncio
import random
import pandas as pd
import json as jsonlib
from datetime import datetime, timedelta
from typing import TypedDict
from aiolimiter import AsyncLimiter


import httpx
from pandas.core.generic import gc

ALCHEMY_RPC_URL = os.environ["ALCHEMY_RPC_URL"]
CHAINSTACK_RPC_URL = os.environ["CHAINSTACK_RPC_URL"]
QUICKNODE_RPC_URL = os.environ["QUICKNODE_RPC_URL"]

START_BLOCK = 19180000

NUM_BLOCKS = 1000


def create_h1_client():
    return httpx.AsyncClient(
        timeout=httpx.Timeout(15),
        limits=httpx.Limits(
            max_keepalive_connections=None,
            max_connections=None,
        ),
    )


def create_h2_client():
    return httpx.AsyncClient(
        http2=True,
        timeout=httpx.Timeout(15),
        limits=httpx.Limits(
            max_keepalive_connections=None,
            max_connections=None,
        ),
    )


def create_h1_clients(n):
    return [create_h1_client() for _ in range(0, n)]


def create_h2_clients(n):
    return [create_h2_client() for _ in range(0, n)]


async def rpc_call(method, params, *, url, client: httpx.AsyncClient):
    if isinstance(client, list):
        client = random.choice(client)

    error = None

    start = datetime.now()

    try:
        res = await client.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params,
            },
        )

        print(res.text[:100])
        res.raise_for_status()
    except (httpx.RequestError, httpx.HTTPError, h2.exceptions.ProtocolError) as e:
        res = None
        error = repr(e)

    end = datetime.now()

    print(f"{method}({params}) done in {end - start}")

    if res:
        error = res.json().get("error", None)

        if isinstance(error, dict):
            error = error.get("message")

    if error:
        print(f"An error occured for {method}({params})", error)

    return res, {
        "start": start,
        "end": end,
        "error": error,
    }


async def test_limit(label, blocks: range, limit, client, url, use_hex=False):
    print(f"Testing limit {label} blocks={blocks}")

    gc.collect()

    sem = AsyncLimiter(limit)

    async def throttled_fn(*args, **kwargs):
        async with sem:
            res = await rpc_call(*args, **kwargs)

            return res

    tasks = [
        throttled_fn(
            "eth_getBlockByNumber",
            [hex(i) if use_hex else i, False],
            url=url,
            client=client,
        )
        for i in blocks
    ]

    calls = await asyncio.gather(*tasks)

    stats = [c[1] for c in calls]

    stats_df = pd.DataFrame(stats)

    stats_df.to_csv(f"output/{label}.csv")


async def test_flood(label, blocks: range, concurrency, client, url, use_hex=False):
    print(f"Testing {label} blocks={blocks} concurrency={concurrency}")

    gc.collect()

    sem = asyncio.Semaphore(concurrency)

    async def throttled_fn(*args, **kwargs):
        async with sem:
            res = await rpc_call(*args, **kwargs)

            return res

    tasks = [
        throttled_fn(
            "eth_getBlockByNumber",
            [hex(i) if use_hex else i, False],
            url=url,
            client=client,
        )
        for i in blocks
    ]

    calls = await asyncio.gather(*tasks)

    stats = [c[1] for c in calls]

    stats_df = pd.DataFrame(stats)

    stats_df.to_csv(f"output/{label}.csv")


async def test_flood_protocols(url, prefix, *, concurrency=1000, use_hex=False):
    await test_flood(
        f"{prefix},b={NUM_BLOCKS},c={concurrency},p=h1,i=1",
        range(START_BLOCK, START_BLOCK + NUM_BLOCKS),
        concurrency,
        create_h1_client(),
        url,
        use_hex,
    )

    await asyncio.sleep(60)

    await test_flood(
        f"{prefix},b={NUM_BLOCKS},c={concurrency},p=h1,i=3",
        range(START_BLOCK, START_BLOCK + NUM_BLOCKS),
        concurrency,
        create_h1_clients(3),
        url,
        use_hex,
    )

    await asyncio.sleep(60)

    await test_flood(
        f"{prefix},b={NUM_BLOCKS},c={concurrency},p=h2,i=1",
        range(START_BLOCK, START_BLOCK + NUM_BLOCKS),
        concurrency,
        create_h2_client(),
        url,
        use_hex,
    )

    await asyncio.sleep(60)

    await test_flood(
        f"{prefix},b={NUM_BLOCKS},c={concurrency},p=h2,i=3",
        range(START_BLOCK, START_BLOCK + NUM_BLOCKS),
        concurrency,
        create_h2_clients(3),
        url,
        use_hex,
    )


async def test_flood_protocols_all_providers(concurrency=100):
    await test_flood_protocols(
        ALCHEMY_RPC_URL,
        "alchemy",
        use_hex=True,
        concurrency=concurrency,
    )

    await asyncio.sleep(15)

    await test_flood_protocols(
        CHAINSTACK_RPC_URL,
        "chainstack",
        concurrency=concurrency,
    )

    await asyncio.sleep(15)

    await test_flood_protocols(
        QUICKNODE_RPC_URL,
        "quicknode",
        use_hex=True,
        concurrency=concurrency,
    )


async def test_chainstack_concurrency():
    for n in range(98, 103):
        prefix = "chainstack,concurrency"

        await test_flood(
            f"{prefix},limits,b={n},c={n}",
            range(START_BLOCK, START_BLOCK + n),
            n,
            create_h2_clients(1),
            CHAINSTACK_RPC_URL,
        )

        await asyncio.sleep(5)


async def test_limits_all():
    cn = 5

    for provider, url in [
        ("quicknode", QUICKNODE_RPC_URL),
        ("alchemy", ALCHEMY_RPC_URL),
        ("chainstack", CHAINSTACK_RPC_URL),
    ]:
        clients = create_h2_clients(cn)

        async def _call(i, bf=3, clients=clients):
            blocks = bf * i

            await test_flood(
                f"{provider},burst,b={blocks},c={i},p=h2,i={cn}",
                range(START_BLOCK, START_BLOCK + (blocks)),
                i,
                clients,
                url,
                use_hex=(provider != "chainstack"),
            )

            await asyncio.sleep(5)

        # Warm up
        for client in clients:
            await _call(cn, 1, client)

        for i in range(5, 125, 5):
            await _call(i)


async def main():
    # await test_flood_protocols_all_providers(100)
    # await test_flood_protocols_all_providers(1000)
    await test_limits_all()
    # await test_chainstack_concurrency()

    pass


asyncio.run(main())
