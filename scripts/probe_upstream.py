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
    docker compose exec -T api python scripts/probe_upstream.py --models   # 只问"这家认哪些模型 id"

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
from app.services.enrich_providers.openai_compat import strip_fence  # noqa: E402

# 探活用的提问：全 ASCII，不碰终端编码，也最省 token
PROBE_PROMPT = 'reply with JSON only: {"ok": true}'


def list_models() -> int:
    """
    问厂商"现在认哪些模型 id"（OpenAI 惯例的 ``GET {base}/models``）。

    模型名会随厂商演进，而且**旧名常常仍被接受、只是被重定向**到当前模型
    （实测：`deepseek-chat` 照样 200，但响应里的 `model` 是 `deepseek-flash`）。
    所以"我这个名字写错没"这件事，权威答案是这张列表 —— 而不是猜，也不是看文档截图。
    """
    url = f"{settings.enrich_openai_base_url.rstrip('/')}/models"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {settings.enrich_api_key}"})
    print("--- 问厂商：现在认哪些模型 id ---")
    print("请求地址 :", url)
    try:
        with urllib.request.urlopen(request, timeout=settings.enrich_timeout_seconds) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        print(f"✗ 上游返回 HTTP {exc.code}")
        print("上游说：", exc.read().decode("utf-8", "replace")[:400])
        return 1
    except (urllib.error.URLError, TimeoutError) as exc:
        print("✗ 没问到：", getattr(exc, "reason", exc))
        return 1

    ids = [item.get("id") for item in (body.get("data") or [])]
    for model_id in ids:
        print("  -", model_id)
    if ids and settings.enrich_model_name not in ids:
        print(f"注意：配置里的 ENRICH_MODEL_NAME={settings.enrich_model_name} 不在上面这张列表里。")
        print("      上游可能仍然接受它（旧名被别名/重定向），写列表里那个更踏实；真不认就直接 400。")
    return 0


def main() -> int:
    if "--models" in sys.argv[1:]:
        return list_models()

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
    served = body.get("model")
    content = (choice.get("message") or {}).get("content")
    print("✓ 通了")
    # 【两个"模型"是两回事，别当成一个】上面那块打的是【我们请求的】，
    # 这里打的是【上游实际服务我们的】（响应体里的 model）。
    # 厂商会把旧名重定向到当前模型（实测：deepseek-chat 会被转给 deepseek-flash），
    # 所以验收"值到底是谁给的"要看这一行。
    print("上游实际服务你的模型（响应里的 model）:", served)
    print("用量 :", body.get("usage"))
    print("内容 :", content)
    if served and served != settings.enrich_model_name:
        print(f"注意：上游把你的请求转给了 {served} —— 多半是 `{settings.enrich_model_name}` 这个旧名被别名/重定向了。")
        print("      以 `GET /models` 列出的 id 为准，或者把 ENRICH_MODEL_NAME 直接写成响应里那个名字。")
    # 【JSON 模式 ≠ 一定合法 JSON】实测真厂商在 response_format=json_object 下也回过
    # `{"ok":AFFIRMATIVE}`（值没加引号）。这里只提示、不算失败：管道里解析失败会回落到规则，
    # 文档照样入库（见 openai_compat 的解析与 enrich_service 的降级链）。
    if content:
        try:
            json.loads(strip_fence(content))     # 用与应用同一个剥围栏函数，判据才一致
        except ValueError:
            print("注意：上面这段内容不是合法 JSON（真厂商在 JSON 模式下也会这样）。")
            print("      管道里会当作「模型没给出可用结果」静默回落到规则，不会写坏数据 —— 这不算故障。")
    if choice.get("finish_reason") == "length":
        print("（注：finish_reason = length —— 探活只给 32 个 token，被截断是正常的）")
        reasoning = ((body.get("usage") or {}).get("completion_tokens_details") or {}).get("reasoning_tokens")
        if reasoning:
            # 实测：带思考的模型（这类会在响应里给 reasoning_tokens）先把预算花在思考上，
            # 于是 32 个 token 可能全被吃掉、连一个字符的内容都没剩下。这不是故障。
            print(f"      这次其中 {reasoning} 个花在思考上（reasoning_tokens）—— 内容为空就是被它吃掉了，别当成故障。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
