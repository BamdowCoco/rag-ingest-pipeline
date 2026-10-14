"""
业务异常 + 统一异常处理器。

为什么不让 service 直接抛 HTTPException：
  - service 是【业务逻辑】，不该知道 HTTP 的存在。同一个 ingest() 将来还要被
    定时任务 / CLI / 消息消费者调用 —— 它们没有「状态码」这种东西。
  - 「业务错误 → HTTP 状态码」的映射集中放在**边界层**（本模块），业务错误保持纯粹。

异常类【不带】 status_code —— 那等于把 HTTP 知识又塞回业务层。映射写在下面的表里。
"""
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse


class AppError(Exception):
    """本项目所有业务异常的基类。"""

    def __init__(self, detail: str = "") -> None:
        # 没传 detail 就退回到子类自己的文档字符串 —— 所以下面每个异常类的 docstring
        # 就是它对外的默认文案。结尾别写句号，免得各处文案不一致。
        self.detail = detail or (self.__class__.__doc__ or "业务错误").strip()
        super().__init__(self.detail)


class DuplicateArticleError(AppError):
    """文章已存在（判重拒收）"""


class ArticleInProgressError(AppError):
    """该文章正在被另一个请求处理，请稍后重试"""


class EmptyContentError(AppError):
    """清洗后正文为空"""


class ArticleNotFoundError(AppError):
    """文章不存在"""


# 业务异常 → HTTP 状态码。这张表只存在于这个（HTTP）边界模块里。
_ERROR_STATUS = {
    ArticleNotFoundError: status.HTTP_404_NOT_FOUND,
    DuplicateArticleError: status.HTTP_409_CONFLICT,
    ArticleInProgressError: status.HTTP_409_CONFLICT,
    EmptyContentError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def register_exception_handlers(app: FastAPI) -> None:
    """把所有 AppError 子类统一转成 {"detail": ...}（与 FastAPI 默认形状一致）。"""
    for exc_type, code in _ERROR_STATUS.items():

        async def handler(request: Request, exc: AppError, _code: int = code) -> JSONResponse:
            # `_code=code` 用默认参数把当前值绑进来。少了它，闭包会把循环结束后的
            # 最后一个 code 用到所有 handler 上 —— 经典闭包坑。
            return JSONResponse(status_code=_code, content={"detail": exc.detail})

        app.add_exception_handler(exc_type, handler)
