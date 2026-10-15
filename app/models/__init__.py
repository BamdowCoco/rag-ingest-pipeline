"""把五个模型 import 进来 —— init_db.init_models() 靠这里把表注册进 Base.metadata。"""
from app.models.document_chunk import DocumentChunk
from app.models.document_clean import DocumentClean
from app.models.document_dedup import DocumentDedup
from app.models.document_raw import DocumentRaw
from app.models.document_source import DocumentSource

__all__ = ["DocumentRaw", "DocumentClean", "DocumentDedup", "DocumentChunk", "DocumentSource"]
