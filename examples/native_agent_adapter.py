"""演示原生代理的输入输出交接格式。"""
import json
import sys


request = json.load(sys.stdin)
print(json.dumps({'items': [], 'source_requests': [],
                  'notes': ['已读取角色' + request['role'] + '。在此接入自己的代理调用并返回实际交付。']}, ensure_ascii=False))
