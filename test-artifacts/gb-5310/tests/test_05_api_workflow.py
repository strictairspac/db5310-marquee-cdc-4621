"""端到端验收：登录鉴权、队列执行、证据校验码、复核签署与导出。

被测 FastAPI 服务运行在独立子进程（自带隔离的环境变量与连接池），
通过 HTTP 访问；MySQL 地址取 GB5310_TEST_MYSQL_URL，Redis 取
GB5310_TEST_REDIS_URL（缺省 redis://127.0.0.1:16379/0），不可用则跳过。
"""

import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import redis

BACKEND_ROOT = Path(__file__).resolve().parents[3] / 'backend'
from support.store import require_mysql_url

# 导入应用配置前补齐占位环境变量；被测服务真正使用的环境由子进程独立传入。
_mysql_url = require_mysql_url()
os.environ.setdefault('AUTH_KEY', 'gb5310-acceptance-key')
os.environ.setdefault('REDIS_URL', os.environ.get('GB5310_TEST_REDIS_URL', 'redis://127.0.0.1:16379/0'))
os.environ.setdefault('DB_DRIVER', 'mysql+pymysql')
os.environ.setdefault('DB_HOST', '127.0.0.1')
os.environ.setdefault('DB_PORT', _mysql_url.split('@')[1].split('/')[0].split(':')[1])
os.environ.setdefault('DB_NAME', 'workflow')
os.environ.setdefault('DB_USER', 'app')
os.environ.setdefault('DB_PASSWORD', 'local-db-demo')


def _free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class _ServerProcess:
    def __init__(self, port, mysql_url, redis_url):
        env = {
            'PATH': os.environ.get('PATH', ''),
            'PYTHONPATH': str(BACKEND_ROOT),
            'DB_DRIVER': 'mysql+pymysql',
            'DB_HOST': '127.0.0.1',
            'DB_PORT': mysql_url.split('@')[1].split('/')[0].split(':')[1],
            'DB_NAME': 'workflow',
            'DB_USER': 'app',
            'DB_PASSWORD': 'local-db-demo',
            'AUTH_KEY': 'gb5310-acceptance-key',
            'REDIS_URL': redis_url,
            'SEED_DEMO': 'false',
        }
        self.process = subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'src.main:app',
             '--host', '127.0.0.1', '--port', str(port), '--log-level', 'warning'],
            cwd=BACKEND_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.port = port
        self.env = env

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        if self.process.stdout:
            self.process.stdout.close()

    def wait_ready(self):
        last = None
        for _ in range(80):
            if self.process.poll() is not None:
                raise RuntimeError(f'服务提前退出：\n{self.process.stdout.read()}')
            try:
                if self.request('GET', '/health')[0] == {'ok': True}:
                    return
            except Exception as exc:
                last = exc
            time.sleep(0.25)
        self.stop()
        raise RuntimeError(f'后端未就绪：{last}\n服务输出：\n{self.process.stdout.read()}')

    def request(self, method, path, token=None, body=None):
        data = None if body is None else json.dumps(body).encode('utf-8')
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', data=data, method=method)
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        if token:
            req.add_header('Authorization', f'Bearer {token}')
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                raw, status, headers = response.read(), response.status, response.headers
        except urllib.error.HTTPError as exc:
            raw, status, headers = exc.read(), exc.code, exc.headers
        parsed = json.loads(raw) if raw else None
        return parsed, status, headers


class ApiWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.mysql_url = require_mysql_url()
        self.redis_url = os.environ.get('GB5310_TEST_REDIS_URL', 'redis://127.0.0.1:16379/0')
        probe = redis.from_url(self.redis_url, socket_timeout=2)
        try:
            probe.ping()
        except Exception as exc:
            raise unittest.SkipTest(f'Redis 不可用（{self.redis_url}）：{exc}')
        finally:
            # 关闭探测连接：避免打开的 socket 在派生服务子进程时被继承而干扰其事件循环。
            probe.close()

        self.port = _free_port()
        # 父进程此前用例借用的连接可能仍持有会话级临时表与元数据锁；
        # 启动 HTTP 子进程前彻底断开，避免其健康检查被父连接阻塞。
        from src.config.settings import engine as parent_engine
        parent_engine.dispose()
        self.password = 'Demo-Walkthrough-2026'
        self.server = _ServerProcess(self.port, self.mysql_url, self.redis_url)
        try:
            self.server.wait_ready()
            self._seed_users()
        except Exception:
            self.server.stop()
            raise

    def tearDown(self):
        self.server.stop()

    def _seed_users(self):
        sys.path.insert(0, str(BACKEND_ROOT))
        from sqlalchemy import create_engine, insert
        from src.models.tables import schema, users
        from src.services.security import hash_password
        engine = create_engine(self.mysql_url, future=True)
        schema.drop_all(engine)
        schema.create_all(engine)
        with engine.begin() as conn:
            for username, role in (('operator', 'operator'), ('reviewer', 'reviewer'), ('viewer', 'viewer')):
                conn.execute(insert(users).values(
                    username=username, role=role,
                    password_hash=hash_password(self.password)))
        engine.dispose()

    def call(self, method, path, token=None, body=None):
        return self.server.request(method, path, token, body)

    def login(self, username):
        body, status, _ = self.call('POST', '/login',
                                    body={'username': username, 'password': self.password})
        self.assertEqual(status, 200)
        return body['token']

    def test_full_workflow_and_role_permissions(self):
        operator = self.login('operator')
        reviewer = self.login('reviewer')
        viewer = self.login('viewer')

        self.assertEqual(self.call('GET', '/cases')[1], 401)
        self.assertEqual(self.call('POST', '/cases', token=reviewer,
                                   body={'title': '越权', 'payload': {}})[1], 403)
        self.assertEqual(self.call('POST', '/cases', token=viewer,
                                   body={'title': '越权', 'payload': {}})[1], 403)

        from src.services.domain import SPEC
        case, status, _ = self.call('POST', '/cases', token=operator,
                                    body={'title': '验收样例', 'payload': SPEC['sample']})
        self.assertEqual(status, 201)
        self.assertEqual(self.call('POST', '/cases', token=operator,
                                   body={'title': '坏档案',
                                         'payload': {'checkpoints': [], 'snapshot': [], 'events': []}})[1], 422)

        run, status, _ = self.call('POST', f"/cases/{case['id']}/runs", token=operator)
        self.assertEqual(status, 202)
        self.assertEqual(self.call('POST', f"/cases/{case['id']}/runs", token=viewer)[1], 403)

        self._process_queue(run['id'])

        runs, status, _ = self.call('GET', '/runs', token=operator)
        self.assertEqual(status, 200)
        self.assertEqual(next(row for row in runs if row['id'] == run['id'])['status'], 'complete')

        evidence_rows, _, _ = self.call('GET', '/evidence', token=operator)
        evidence = next(row for row in evidence_rows if row['run_id'] == run['id'])
        self.assertEqual(evidence['report']['metrics'], SPEC['expect'])

        import hashlib
        from src.utils.encoding import canonical
        expected_digest = hashlib.sha256(canonical(evidence['report'])).hexdigest()
        self.assertEqual(evidence['digest'], expected_digest)

        # 重复执行不新增收件：报告与校验码完全一致。
        rerun, _, _ = self.call('POST', f"/cases/{case['id']}/runs", token=operator)
        self._process_queue(rerun['id'])
        evidence_rows, _, _ = self.call('GET', '/evidence', token=operator)
        rerun_evidence = next(row for row in evidence_rows if row['run_id'] == rerun['id'])
        self.assertEqual(rerun_evidence['digest'], expected_digest)
        self.assertEqual(rerun_evidence['report'], evidence['report'])

        exported, status, headers = self.call(
            'GET', f"/evidence/{evidence['id']}/download", token=viewer)
        self.assertEqual(status, 200)
        self.assertEqual(exported, evidence)
        self.assertIn('attachment', headers.get('Content-Disposition', ''))
        self.assertEqual(headers.get('Content-Type'), 'application/json')

        self.assertEqual(self.call('POST', f"/evidence/{evidence['id']}/review", token=viewer,
                                   body={'note': '越权签署'})[1], 403)
        reviewed, status, _ = self.call('POST', f"/evidence/{evidence['id']}/review", token=reviewer,
                                        body={'note': '矛盾两版已核对，南区 6 号待上游补传。'})
        self.assertEqual(status, 200)
        self.assertEqual(reviewed['reviewer'], 'reviewer')
        self.assertEqual(self.call('POST', f"/evidence/{evidence['id']}/review", token=reviewer,
                                   body={'note': '重复签署'})[1], 409)

    def _process_queue(self, run_id, timeout=60):
        """驱动持久队列直到指定执行进入终态。

        部署中由常驻 worker 轮询；这里以子进程复用同一套 claim/finish 逻辑。
        即使存在其他队列消费者抢先领取，也只以执行终态为准，避免验收用例
        与真实 worker 抢锁而误报。
        """
        script = f'''
import sys
sys.path.insert(0, {str(BACKEND_ROOT)!r})
from src.config.settings import engine
from src.services.workflow import claim, finish
from src.services.domain import solve
deadline = {timeout}
import time
end = time.time() + deadline
while time.time() < end:
    job = claim()
    if not job:
        time.sleep(0.1)
        continue
    rid, token, payload = job
    try:
        finish(rid, token, result=solve(payload, engine))
    except Exception as exc:
        finish(rid, token, error=str(exc)[:500])
'''
        worker = subprocess.Popen([sys.executable, '-c', script], cwd=BACKEND_ROOT,
                                  env={**self.server.env, 'PATH': os.environ.get('PATH', '')},
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            token = self.login('operator')
            deadline = time.time() + timeout
            while time.time() < deadline:
                runs, _, _ = self.call('GET', '/runs', token=token)
                row = next(item for item in runs if item['id'] == run_id)
                if row['status'] in ('complete', 'failed'):
                    self.assertEqual(row['status'], 'complete', row.get('error'))
                    return
                time.sleep(0.2)
            self.fail(f'执行 {run_id} 在 {timeout}s 内未进入终态')
        finally:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
