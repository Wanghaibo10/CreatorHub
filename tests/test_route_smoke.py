"""路由装配冒烟:防「注解解析失败 → body 退化成 query 参数」这类静默故障。

2026-08-17 整改审查抓到过一次:模块拆分把 Pydantic 模型搬走后,留在原地的
端点因 `from __future__ import annotations` 字符串注解解析失败,FastAPI 把
body 参数静默退化成了 query 必填参数——启动不报错,接口必挂(422)。
"""
import tempfile
import unittest

from fastapi.params import Body, Query
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import moss.common.db as db
from app.main import app


class RouteAssemblyTests(unittest.TestCase):
    def test_no_body_param_degraded_to_query(self):
        """任何路由都不该出现名叫 body 的 query 参数——那是注解解析失败的指纹。"""
        bad = []
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            for dep in route.dependant.query_params:
                if dep.name == "body":
                    bad.append(f"{sorted(route.methods)} {route.path}")
        self.assertEqual(bad, [], f"这些路由的 body 退化成了 query 参数: {bad}")

    def test_route_paths_have_no_identifier_artifacts(self):
        """路径里不该混进代码标识符(如批量替换事故留下的 rt.)。"""
        bad = [r.path for r in app.routes
               if isinstance(r, APIRoute) and ("/rt." in r.path or " " in r.path)]
        self.assertEqual(bad, [])


class RouteSmokeTests(unittest.TestCase):
    """不跑 lifespan(引擎/浏览器为 None),只验证出入参装配与查库路径。"""

    def setUp(self):
        self._previous_engine = db._engine
        self._tmp = tempfile.TemporaryDirectory()
        db.init_db(f"{self._tmp.name}/smoke.db")
        self.client = TestClient(app)

    def tearDown(self):
        db._engine = self._previous_engine
        self._tmp.cleanup()

    def test_account_proxy_body_is_parsed(self):
        # 曾经的阻断故障:ProxyIn 注解失效时这里回 422 而不是 404
        r = self.client.put("/api/accounts/999999/proxy",
                            json={"proxy": "http://1.2.3.4:8080"})
        self.assertEqual(r.status_code, 404, r.text)

    def test_all_domain_lists_respond(self):
        for path in ("/api/accounts", "/api/proxies", "/api/settings",
                     "/api/notifications", "/api/comment-rules", "/api/monitors",
                     "/api/contents", "/api/comment-watches", "/api/danmaku-watches",
                     "/api/collections", "/api/publish", "/api/share-download/history",
                     "/health"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_login_paths_survived_refactor(self):
        # 批量替换事故曾把 /api/login/browser/start 弄成 /api/login/rt.browser/start
        r = self.client.post("/api/login/browser/start", json={})
        self.assertEqual(r.status_code, 503)   # 浏览器未就绪,而不是 404

    def test_article_browser_login_route(self):
        # 图文平台浏览器登录入口:浏览器未就绪时 503(而不是 404/422)
        r = self.client.post("/api/login/article/start?platform=baijiahao")
        self.assertEqual(r.status_code, 503, r.text)

    def test_collection_create_and_retry_survive_engine_none(self):
        # 曾经的阻断故障:rt 迁移把这两处漏成 `if engine:`(未定义名),POST 必 500。
        # rt.engine 为 None 时不 enqueue,但接口本身必须活着。
        from moss.model import DouyinAccount, KeywordCollectionJob
        from moss.common.db import get_session
        with get_session() as s:
            acc = DouyinAccount(platform="douyin", nickname="冒烟",
                                status="active", storage_state="{}")
            s.add(acc); s.commit(); s.refresh(acc)
            acc_id = acc.id
        r = self.client.post("/api/collections", json={
            "platform": "douyin", "account_id": acc_id, "keywords": ["冒烟词"]})
        self.assertEqual(r.status_code, 200, r.text)
        job_id = r.json()["id"]
        with get_session() as s:
            job = s.get(KeywordCollectionJob, job_id)
            job.status = "failed"
            s.add(job); s.commit()
        r2 = self.client.post(f"/api/collections/{job_id}/retry")
        self.assertEqual(r2.status_code, 200, r2.text)


if __name__ == "__main__":
    unittest.main()
