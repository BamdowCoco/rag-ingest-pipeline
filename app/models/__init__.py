"""把三个模型 import 进来 —— init_db.init_models() 靠这里把表注册进 Base.metadata。"""
from app.models.article_clean import ArticleClean
from app.models.article_dedup import ArticleDedup
from app.models.article_raw import ArticleRaw

__all__ = ["ArticleRaw", "ArticleClean", "ArticleDedup"]
