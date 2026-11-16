"""读取调用方指定的动作与报价，输出独立执行预检。"""
import argparse
import json

from pm_paper.execution import preflight
from pm_paper.files import read_json


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--item', required=True)
parser.add_argument('--snapshot', required=True)
parser.add_argument('--cash')
args = parser.parse_args()
print(json.dumps(preflight(read_json(args.item), read_json(args.snapshot), args.cash), ensure_ascii=False, indent=2))
