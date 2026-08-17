# -*- coding: utf-8 -*-
"""
@Author         : Qoder
@CreateTime     : 2026/1/29
@ClassName      : async_session
@Version        : 1.0.0
@Describe       : 异步 HTTP 客户端基类，基于 curl_cffi.requests.AsyncSession
                  提供与 MCurlSession 一致的接口风格和代理管理机制

⚠️ 本文件从 moss/common/requests_extension/async_session.py 原样引入（2026-08-17），
   是上游共用基座，**不要在这里加 CreatorHub 的业务逻辑**——平台相关的凭证、
   风控、错误分类一律写在 app/platforms/base.py 的子类里，这样上游更新可直接覆盖。
"""
import json
import logging
import asyncio
from logging import Logger
from typing import Callable, Optional, Dict

from curl_cffi.requests import AsyncSession, Response as CurlResponse, BrowserType


class AsyncMCurlSession:
    """
    异步版本的 MCurlSession 基类
    
    特性：
    - 基于 curl_cffi.requests.AsyncSession 实现非阻塞请求
    - 支持 proxies_refresh 回调动态获取代理
    - 内置重试机制和指数退避
    - 支持请求/响应回调钩子
    - 与 MCurlSession 保持一致的接口风格
    
    使用方式：
        async with MyAsyncClient(...) as client:
            response = await client.get("https://example.com")
    """
    json = json
    Impersonate = BrowserType

    def __init__(
        self,
        session_id: str = 'AsyncMCurlSession',
        proxies_refresh: Optional[Callable] = None,
        logger: Logger = logging.getLogger(),
        unique_id: Optional[str] = None,
        retries: int = 1,
        backoff_factor: float = 0,
        timeout: Optional[int] = 120,
        enable_logger: bool = True,
        impersonate: str = "chrome116",
        verify: bool = False,
        **kwargs
    ) -> None:
        """
        初始化异步会话
        
        Args:
            session_id: 会话标识，用于日志输出
            proxies_refresh: 代理刷新回调函数，返回 dict 格式 {"http": "...", "https": "..."}
            logger: 日志记录器
            unique_id: 唯一标识
            retries: 重试次数
            backoff_factor: 指数退避因子
            timeout: 请求超时时间(秒)
            enable_logger: 是否启用日志
            impersonate: 浏览器模拟类型
            verify: 是否验证 SSL 证书
            **kwargs: 传递给 AsyncSession 的其他参数
        """
        self._proxies_refresh = proxies_refresh
        self._session_id = session_id
        self._logger = logger
        self._retries = retries if retries > 0 else 1
        self._backoff_factor = backoff_factor
        self._enable_logger = enable_logger
        self._timeout = timeout
        self._unique_id = unique_id
        self._impersonate = impersonate
        self._verify = verify
        self._session_kwargs = kwargs
        
        # 代理缓存
        self._proxies: Optional[Dict[str, str]] = None
        
        # 会话实例（在 __aenter__ 中创建）
        self.session: Optional[AsyncSession] = None
        
        # 初始化代理
        self._init_proxies()
        
        # 调用子类初始化钩子
        self.__post_init__()

    def __post_init__(self):
        """子类初始化钩子，供子类重写"""
        pass

    def _init_proxies(self):
        """初始化代理配置"""
        if self._proxies_refresh:
            self._proxies = self._proxies_refresh()
            if self._enable_logger and self._proxies:
                self._logger.info(f'[{self._session_id}][初始化代理][{self._proxies}]')

    @property
    def logger(self) -> Logger:
        return self._logger

    @property
    def unique_id(self) -> Optional[str]:
        return self._unique_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def proxies(self) -> Optional[Dict[str, str]]:
        return self._proxies

    @proxies.setter
    def proxies(self, value: Optional[Dict[str, str]]):
        self._proxies = value

    def proxies_refresh(self, *args, **kwargs) -> Optional[Dict[str, str]]:
        """
        刷新代理
        """
        if not self._proxies_refresh:
            return self._proxies
        self._proxies = self._proxies_refresh(*args, **kwargs)
        if self._enable_logger:
            self._logger.info(f'[{self._session_id}][刷新代理][{self._proxies}]')
        return self._proxies

    async def __aenter__(self) -> 'AsyncMCurlSession':
        """异步上下文管理器入口"""
        session_kwargs = {
            'impersonate': self._impersonate,
            'timeout': self._timeout,
            'verify': self._verify,
            **self._session_kwargs
        }
        
        # 如果有代理，添加到 session 初始化参数
        if self._proxies:
            session_kwargs['proxies'] = self._proxies
            
        self.session = AsyncSession(**session_kwargs)
        
        # 调用子类的会话初始化钩子
        await self._on_session_created()
        
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出"""
        if self.session:
            await self.session.close()
            self.session = None

    async def _on_session_created(self):
        """会话创建后的钩子，供子类重写"""
        pass

    async def request(
        self,
        method: str,
        url: str,
        allow_redirects: bool = True,
        **kwargs
    ) -> CurlResponse:
        """
        发送 HTTP 请求，带重试机制
        
        Args:
            method: HTTP 方法 (GET, POST, etc.)
            url: 请求 URL
            allow_redirects: 是否允许重定向
            **kwargs: 传递给 session.request 的其他参数
            
        Returns:
            响应对象
            
        Raises:
            Exception: 重试次数耗尽后抛出最后一次异常
        """
        if not self.session:
            raise RuntimeError("Session not initialized. Use 'async with' context manager.")
            
        retry_count = 0
        timeout = kwargs.pop('timeout', None) or self._timeout
        kwargs['timeout'] = timeout
        kwargs['allow_redirects'] = allow_redirects

        while True:
            try:
                if self._enable_logger:
                    self._logger.info(f'[{self._session_id}][send]: {method} {url}')
                
                # 请求前回调
                await self.request_callback(method, url, **kwargs)
                
                # 发送请求
                response = await self.session.request(method, url, **kwargs)
                
                # 响应后回调
                response = await self.response_callback(response, method, url, **kwargs)
                
                if self._enable_logger:
                    self._logger.info(f'[{self._session_id}][send OK]: {method} {url} {response.status_code}')
                
                return response
                
            except Exception as e:
                await self.request_fail_callback(e, method, url, **kwargs)
                if self._enable_logger:
                    self._logger.warning(f'[{self._session_id}][send ERROR] {method} {url} retry: {retry_count}, error: {e}')
                    
                if retry_count >= self._retries:
                    raise e
                    
                retry_count += 1
                if self._backoff_factor:
                    backoff_time = self._backoff_factor * (2 ** (retry_count - 1))
                    await asyncio.sleep(backoff_time)

    async def request_callback(self, method: str, url: str, **kwargs):
        """请求前回调钩子，供子类重写"""
        pass

    async def response_callback(
        self,
        response: CurlResponse,
        method: str,
        url: str,
        **kwargs
    ) -> CurlResponse:
        """响应后回调钩子，供子类重写"""
        return response

    async def request_fail_callback(self, e: Exception, method: str, url: str, **kwargs):
        """请求失败回调钩子，供子类重写"""
        pass

    def clear_cookie(self):
        """清除所有 cookies"""
        if self.session:
            self.session.cookies.clear()

    def cookies_dict(self, domain: str = None, path: str = None) -> Dict[str, str]:
        """
        获取 cookies 字典
        """
        if not self.session:
            return {}
        dictionary = {}
        for cookie in self.session.cookies:
            # 兼容 Cookie 对象和字符串两种情况
            if hasattr(cookie, 'name') and hasattr(cookie, 'value'):
                # Cookie 对象
                if (domain is None or getattr(cookie, 'domain', None) == domain) and \
                   (path is None or getattr(cookie, 'path', None) == path):
                    dictionary[cookie.name] = cookie.value
            elif isinstance(cookie, str):
                # 字符串 key，从 jar 中获取 value
                dictionary[cookie] = self.session.cookies.get(cookie, '')
        return dictionary

    # HTTP 方法快捷方式
    async def get(self, url: str, **kwargs) -> CurlResponse:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> CurlResponse:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs) -> CurlResponse:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str, **kwargs) -> CurlResponse:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs) -> CurlResponse:
        return await self.request("DELETE", url, **kwargs)

    async def head(self, url: str, **kwargs) -> CurlResponse:
        return await self.request("HEAD", url, **kwargs)

    async def options(self, url: str, **kwargs) -> CurlResponse:
        return await self.request("OPTIONS", url, **kwargs)
