"""gb-5310 验收测试的公共夹具。

测试通过环境变量 ``GB5310_TEST_MYSQL_URL`` 取得独立 MySQL 8.0 schema，
未配置时整个模块跳过（``BUSINESS.md`` 约定该账号需要 CREATE TEMPORARY TABLES 权限）。
"""

import os
import unittest
from itertools import groupby
from operator import itemgetter

from sqlalchemy import create_engine

ENV_URL = 'GB5310_TEST_MYSQL_URL'


def require_mysql_url():
    url = os.environ.get(ENV_URL)
    if not url:
        raise unittest.SkipTest(f'未设置 {ENV_URL}，跳过需要 MySQL 8.0 的验收用例')
    return url


def make_engine():
    # 验收用例每次回放都借用独立连接；禁用池化，避免长连接持有的会话级临时表
    # 与表锁波及后续用例及子进程服务的健康检查。
    from sqlalchemy.pool import NullPool
    return create_engine(require_mysql_url(), poolclass=NullPool, future=True)


def events_by_batch(rows):
    """按批号切分领域层内部使用的到货事件结构，保留同批输入行序。"""
    from src.services.domain import _arrival_events
    grouped = {}
    for batch, events in groupby(_arrival_events(rows), key=itemgetter('batch_no')):
        grouped[batch] = list(events)
    return grouped


def checkpoints(*pairs):
    return [{'stream': stream, 'sequence': sequence} for stream, sequence in pairs]


def event(batch, stream, sequence, sign_id, content, *, event_id=None, operation='put'):
    return {'batch': batch, 'stream': stream, 'sequence': sequence,
            'event_id': event_id or f'E-{sequence}', 'sign_id': sign_id,
            'operation': operation, 'content': content}
