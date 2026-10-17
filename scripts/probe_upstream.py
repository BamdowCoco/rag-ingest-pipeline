"""
上游探活：单独验一次"厂商 API 通不通"，把「接法不对」和「管道不通」分开。

【什么时候用它】接了新厂商 / 换了 key / 换模型时 **先跑这个** ：它只发一次极小的请求，
不碰数据库、不碰管道。这样失败时你立刻知道是"上游或配置的问题"还是"管道的问题" ——
管道那边失败长得完全不一样（文档照样 201 入库、几列静默回落到规则、日志里一行"回落到规则"）。

【它验的就是应用真正用的那套配置】直接读 ``app.core.config.settings``（provider、基址、
端点路径、模型名、key、JSON 模式、超时），所以和容器里跑出来的行为一致。

【绝不打印 key】只打"配没配"这个布尔 —— 打到终端或日志里的东西，迟早会被顺手贴出去。

跑法（在已经跑着的 api 容器里）：

    docker compose exec -T api python scripts/probe_upstream.py

失败时退出码是 1，方便脚本化判断。
"""
import json
import pathlib
import sys
import urllib.error
import urllib.request

# 让这个脚本既能 `python scripts/probe_upstream.py` 跑、也能 `python -m scripts.probe_upstream` 跑
# （前者只会把 scripts/ 放进 sys.path，那样 import app 会失败）
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402  —— 必须在上面那行之后才能 import

# 探活用的提问：全 ASCII，不碰终端编码，也最省 token
PROBE_PROMPT = 'reply with JSON only: {"ok": true}'


def main() -> int:
    if settings.enrich_provider != "openai_compat":
        # 【只是提示，不拦】本脚本验的是 openai_compat 那一档的配置（地址/模型/key），
        # 而配置与"当前用哪一档"无关 —— 所以先探活、通了再切 provider 是完全可以的（README 就是这个顺序）。
        print(f"提示：当前 provider = {settings.enrich_provider}，而本脚本验的是 openai_compat 那一档的配置。")
        print("      要让管道真的走它：把 .env 的 ENRICH_PROVIDER 改成 openai_compat，再 `docker compose up -d api`。")
        print()

    url = f"{settings.enrich_openai_base_url.rstrip('/')}/{settings.enrich_openai_path.lstrip('/')}"
    print("--- 将要用的配置 ---")
    print("请求地址 :", url)
    print("模型     :", settings.enrich_model_name)
    print("JSON 模式:", settings.enrich_json_mode)
    print("超时     :", settings.enrich_timeout_seconds, "秒")
    print("key      :", "已配置" if settings.enrich_api_key else "★ 没配！ENRICH_API_KEY 是空的")

    payload = {
        "model": settings.enrich_model_name,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "max_tokens": 32,
    }
    if settings.enrich_json_mode:
        payload["response_format"] = {"type": "json_object"}

    request = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.enrich_api_key}",
        },
    )

    print("--- 发一次极小请求 ---")
    try:
        with urllib.request.urlopen(request, timeout=settings.enrich_timeout_seconds) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # 上游回了 4xx/5xx —— 把它自己的错误体原样打出来：
        # 401 会写着 "Authentication Fails"，400 会指出哪个参数不对。这比一段 traceback 有用得多。
        print(f"✗ 上游返回 HTTP {exc.code}")
        print("上游说：", exc.read().decode("utf-8", "replace")[:600])
        return 1
    except urllib.error.URLError as exc:
        print("✗ 连不上上游：", exc.reason)
        print("  查一下：地址拼错没、容器能不能出网、有没有公司代理/防火墙拦着")
        return 1
    except TimeoutError:
        print(f"✗ 超时（{settings.enrich_timeout_seconds} 秒）")
        print("  云厂商冷启动有时会慢 —— 把 ENRICH_TIMEOUT_SECONDS 调大再试")
        return 1

    choice = (body.get("choices") or [{}])[0]
    print("✓ 通了")
    print("模型 :", body.get("model"))
    print("用量 :", body.get("usage"))
    print("内容 :", (choice.get("message") or {}).get("content"))
    if choice.get("finish_reason") == "length":
        print("（注：finish_reason = length —— 探活只要 32 个 token，被截断是正常的）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
