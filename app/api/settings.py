"""全局设置 API。

从 main.py 抽出(2026-08-17 模块化)。键值存取在 app/settings.py,
QUALITY_CHOICES 同步上移到那里(分享链接下载域也用)。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from moss.core.config import get_config
from moss.core.settings import QUALITY_CHOICES, get_setting, set_setting

router = APIRouter(tags=["settings"])
cfg = get_config()


class SettingsIn(BaseModel):
    download_dir: str | None = None
    video_quality: str | None = None
    # 大模型 API 文案生成(自动评论用;OpenAI 兼容接口)
    ai_enabled: bool | None = None
    ai_base_url: str | None = None
    ai_api_key: str | None = None        # 留空=不改(避免误清空已存的 key)
    ai_model: str | None = None
    ai_prompt: str | None = None
    ai_temperature: str | None = None


def _settings_dict() -> dict:
    return {
        "download_dir": get_setting("download_dir", cfg.engine.media_dir),
        "video_quality": get_setting("video_quality", "highest"),
        "ai_enabled": get_setting("ai_enabled", "0") == "1",
        "ai_base_url": get_setting("ai_base_url", ""),
        "ai_model": get_setting("ai_model", ""),
        "ai_prompt": get_setting("ai_prompt", ""),
        "ai_temperature": get_setting("ai_temperature", "0.9"),
        # 不回传明文 key,只告知是否已配置
        "ai_api_key_set": bool(get_setting("ai_api_key", "")),
    }


@router.get("/api/settings")
async def get_settings():
    return _settings_dict()


@router.put("/api/settings")
async def put_settings(body: SettingsIn):
    if body.download_dir is not None:
        path = body.download_dir.strip()
        if path:
            try:
                Path(path).expanduser().mkdir(parents=True, exist_ok=True)
            except Exception as e:
                raise HTTPException(400, f"目录不可用: {e}")
        set_setting("download_dir", path)
    if body.video_quality is not None:
        q = body.video_quality.strip() or "highest"
        if q not in QUALITY_CHOICES:
            raise HTTPException(400, f"画质取值无效: {q}")
        set_setting("video_quality", q)
    if body.ai_enabled is not None:
        set_setting("ai_enabled", "1" if body.ai_enabled else "0")
    if body.ai_base_url is not None:
        set_setting("ai_base_url", body.ai_base_url.strip())
    if body.ai_model is not None:
        set_setting("ai_model", body.ai_model.strip())
    if body.ai_prompt is not None:
        set_setting("ai_prompt", body.ai_prompt)
    if body.ai_temperature is not None:
        set_setting("ai_temperature", (body.ai_temperature or "0.9").strip())
    if body.ai_api_key:    # 仅在传了非空值时更新,留空=保留原 key
        set_setting("ai_api_key", body.ai_api_key.strip())
    return _settings_dict()


class AiTestIn(BaseModel):
    # 可选覆盖(便于保存前先测);留空则用已保存设置。key 留空=用已存的
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    prompt: str | None = None
    temperature: str | None = None


@router.post("/api/settings/ai-test")
async def ai_test(body: AiTestIn):
    """用当前(或传入的)AI 配置做一次最小生成,验证连通性。返回 {ok, sample/error}。"""
    from application.engine import compose
    ai = {
        "base_url": body.base_url if body.base_url is not None else get_setting("ai_base_url", ""),
        "api_key": body.api_key if body.api_key else get_setting("ai_api_key", ""),
        "model": body.model if body.model is not None else get_setting("ai_model", ""),
        "prompt": body.prompt if body.prompt is not None else get_setting("ai_prompt", ""),
        "temperature": body.temperature or get_setting("ai_temperature", "0.9"),
        "timeout": 25,
    }
    if not (ai["base_url"] and ai["api_key"] and ai["model"]):
        raise HTTPException(400, "请先填写 Base URL / 模型,并保存或填入 API Key")
    ctx = {"source_text": "这条视频拍得太治愈了,期待更新!", "nick": "测试用户",
           "kw": "", "platform": "douyin", "mode": "auto_reply"}
    try:
        text = await compose.generate(ctx, ai)
        return {"ok": True, "sample": text}
    except Exception as e:
        msg = str(e) or e.__class__.__name__
        return {"ok": False, "error": f"{msg}(检查 Base URL / Key / 模型 / 网络/代理)"}
