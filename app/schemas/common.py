"""通用模型：分页信封。GET /documents 统一返回这个形状，泛型复用。"""
from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int      # 满足条件的总数（不是本页条数）
    page: int       # 当前页（从 1 开始）
    limit: int      # 每页条数
