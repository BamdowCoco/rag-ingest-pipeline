"""统计出参。"""
from pydantic import BaseModel


class SourceCount(BaseModel):
    source: str
    count: int


class ChunkStatusCount(BaseModel):
    """分块的状态分布 —— pending 那部分就是「向量化积压」。"""
    status: str
    count: int


class StatsOut(BaseModel):
    total: int
    by_source: list[SourceCount]   # 按来源 top10
    today_count: int               # 今日新增
    dedup_rejected: int            # 判重拒收累计（Redis 计数器）
    chunk_status: list[ChunkStatusCount]   # 分块状态分布（pending = 向量化积压）
