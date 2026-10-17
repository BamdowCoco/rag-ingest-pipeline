"""
一个「假模型服务」—— **零第三方依赖** （只用 stdlib 的 ``http.server``）。

它就是「 **假上游、真管道** 」里的那个上游：让 enrich 那几档 HTTP provider
**有现场可跑的东西** ，也让 **降级链可验收** —— 把它 stop 掉，入库照样成功。

【它替身的是什么】替身的是 **服务端** ，不是"HTTP 调用"这件事。
``/enrich`` 那个契约由 **我方** 定义（≈ 企业内部 AI 网关的形态）；真实厂商 API 的形状不是这样，
那一层差异由 ``app/services/enrich_providers/`` 里的 **适配器** 负责翻译。

【两个端点：故意讲两种不同的契约】这样"适配层到底在翻译什么"就不用嘴说 ——
**一个上游、两个适配器、两种解析** ：

    POST /enrich               我方契约（网关形态）：{"text": ...} → {summary, keywords, language}
    POST /v1/chat/completions  OpenAI 兼容：messages 数组 → choices[0].message.content（ **JSON 字符串** ）+ usage

结果的算法 **故意和规则那套不一样** （规则是"首段 + 尾段"、jieba 分词；
这里是"从头截一刀"、ngram 词频）—— 这样验收时一眼能看出 **值到底是谁给的** 。
"""
import json
import re
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8000
SUMMARY_CHARS = 120      # 故意比规则侧的 200 短，好区分
TOPN = 5


def summarize(text: str) -> str:
    """假模型：把空白压平、从头截一刀。"""
    return re.sub(r"\s+", " ", text).strip()[:SUMMARY_CHARS]


def extract_keywords(text: str) -> list[str]:
    """假模型：中文按 2-gram、英文按单词，数词频取 top。 **不用 jieba** —— 它只是个桩。"""
    chinese = re.findall(r"[一-鿿]", text)
    grams = ["".join(chinese[i:i + 2]) for i in range(len(chinese) - 1)]
    words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", text)]
    return [word for word, _ in Counter(grams + words).most_common(TOPN)]


def detect_language(text: str) -> str:
    """假模型：看字符集占比粗判 zh / en。"""
    chinese = len(re.findall(r"[一-鿿]", text))
    letters = len(re.findall(r"[A-Za-z]", text))
    return "zh" if chinese >= letters else "en"


def _last_user_message(payload: dict) -> str:
    """按 OpenAI 兼容的取法，从 messages 里取 **最后一条** user 的内容。"""
    for message in reversed(payload.get("messages") or []):
        if message.get("role") == "user":
            return message.get("content") or ""
    return ""


def chat_completion(payload: dict) -> dict:
    """
    OpenAI 兼容形状的响应。

    两处正是适配层要解析的：结果藏在 ``choices[0].message.content`` 里，而且是一段 **JSON 字符串**
    （不是对象）；另附 ``usage`` 供 **记账** —— 真上游给的是真 token 数，这里粗略估算就够了。
    """
    text = _last_user_message(payload)
    content = json.dumps(
        {
            "summary": summarize(text),
            "keywords": extract_keywords(text),
            "language": detect_language(text),
        },
        ensure_ascii=False,
    )
    prompt_tokens = len(text)
    completion_tokens = len(content) // 2

    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": payload.get("model") or "fake-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:              # noqa: N802 —— stdlib 规定的命名
        if self.path not in ("/enrich", "/v1/chat/completions"):
            self.send_error(404, "only POST /enrich and POST /v1/chat/completions")
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_error(400, "invalid json")
            return

        if self.path == "/enrich":
            text = payload.get("text") or ""
            self._send_json(
                {
                    "summary": summarize(text),
                    "keywords": extract_keywords(text),
                    "language": detect_language(text),
                }
            )
        else:
            self._send_json(chat_completion(payload))

    def _send_json(self, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: object) -> None:
        # 默认的访问日志会刷屏，而且对"假服务"没什么价值 —— 关掉
        return


if __name__ == "__main__":
    print("[model] 假模型服务已启动 :8000（POST /enrich、POST /v1/chat/completions）", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
