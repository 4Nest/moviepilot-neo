from typing import Dict, Optional

from pydantic import BaseModel, ConfigDict


class CategoryRule(BaseModel):
    """
    分类规则详情
    """
    # 内容类型
    genre_ids: Optional[str] = None
    # 语种
    original_language: Optional[str] = None
    # 国家或地区（电视剧）
    origin_country: Optional[str] = None
    # 国家或地区（电影）
    production_countries: Optional[str] = None
    # 发行年份
    release_year: Optional[str] = None
    # 拒绝未知字段，避免笔误静默覆写整个配置
    model_config = ConfigDict(extra='forbid')


class CategoryConfig(BaseModel):
    """
    分类策略配置
    """
    # 电影分类策略
    movie: Optional[Dict[str, Optional[CategoryRule]]] = {}
    # 电视剧分类策略
    tv: Optional[Dict[str, Optional[CategoryRule]]] = {}
    # 拒绝未知顶层键（如 moive 笔误），避免静默丢弃全部配置
    model_config = ConfigDict(extra='forbid')


class CategoryRawConfig(BaseModel):
    """
    分类策略配置原文
    """
    # category.yaml 原文内容
    content: str
