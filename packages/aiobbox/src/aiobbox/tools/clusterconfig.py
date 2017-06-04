import os, sys
import re
import json
import asyncio
import argparse
import aiobbox.client as bbox_client
import aiobbox.config as bbox_config
from aiobbox.cluster import ClientAgent
from aiobbox.utils import guess_json, json_pp

parser = argparse.ArgumentParser(
    description='test an rpc interface')

parser.add_argument(
    'op',
    type=str,
    help='config operations')

parser.add_argument(
    'param',
    type=str,
    nargs='*',
    help='params')

async def get_config(sec_key):
    if '/' in sec_key:
        sec, key = sec_key.split('/')
        r = bbox_config.cluster.get_strict(sec, key)
    else:
        r = bbox_config.cluster.get_section_strict(sec_key)
    print(json_pp(r))

async def set_config(sec_key, value):
    sec, key = sec_key.split('/')
    value = guess_json(value)
    return await ClientAgent.agent.set_config(sec, key, value)

async def del_config(sec_key):
    if '/' in sec_key:
        sec, key = sec_key.split('/')
        return await ClientAgent.agent.del_config(sec, key)
    else:
        return await ClientAgent.agent.del_section(sec_key)

async def clear_config():
    return await ClientAgent.agent.clear_config()

async def dump_config():
    data = bbox_config.cluster.dump_json()
    print(data)

async def load_config(jsonfile):
    with open(jsonfile, 'r', encoding='utf-8') as f:
        new_sections = json.load(f)
    rem_set, add_set = bbox_config.cluster.compare_sections(new_sections)
    #print(rem_set, add_set)
    for sec, key, value in rem_set:
        print("delete", sec, key)
        await ClientAgent.agent.del_config(sec, key)
    for sec, key, value in add_set:
        value = json.loads(value)
        print("set", sec, key)
        await ClientAgent.agent.set_config(sec, key, value)

def help(f=sys.stdout):
    print('Commands', file=f)
    print(' get sec.key|sec  - get config or section', file=f)
    print(' set sec.key value  - set config', file=f)
    print(' dump  - dump configs in json format', file=f)
    print(' del sec.key|sec  - delete config or section', file=f)
    print(' clear  - clear configs', file=f)
    print(' load config.json  - clear configs', file=f)

async def main():
    bbox_config.parse_local()
    args = parser.parse_args()
    try:
        await ClientAgent.connect_cluster(**bbox_config.local)

        if args.op == 'get':
            await get_config(*args.param)
        elif args.op == 'set':
            await set_config(*args.param)
        elif args.op == 'del':
            await del_config(*args.param)
        elif args.op == 'clear':
            await clear_config(*args.param)
        elif args.op == 'dump':
            await dump_config(*args.param)
        elif args.op == 'load':
            await load_config(*args.param)
        else:
            help()
    finally:
        if ClientAgent.agent:
            ClientAgent.agent.cont = False
            await asyncio.sleep(0.1)
            ClientAgent.agent.close()

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
