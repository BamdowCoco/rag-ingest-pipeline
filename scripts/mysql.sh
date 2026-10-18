#!/usr/bin/env bash
# 在 mysql 容器里发一句 SQL —— 为什么不直接写 `docker compose exec mysql mysql -e "..."`：
#
# - 【引号地雷】cmd.exe / PowerShell / Git Bash 解析引号的规则各不相同，带括号、星号、分号的 SQL
#   直接拼进命令行会各种翻车（cmd 里 `2>/dev/null` 甚至不被当成"丢掉错误输出"，而是一个文件路径）。
#   这里让【宿主 shell 只传一个带引号的参数】，其余拼接全在本文件里完成。
# - 【口令不出现】口令从 mysql 容器【自己的】环境变量取（compose 已经注入），不用记、不用输、
#   也不进命令行 —— 那句 "Using a password on the command line interface can be insecure" 自然也没了。
#
# 用法（cmd.exe 里前面加 bash 即可）：
#   bash scripts/mysql.sh "select count(*) from document_clean;"
#   bash scripts/mysql.sh < q.sql      # 不给参数时把 stdin 原样喂给 mysql（多语句 / 大 SQL）
#
# 详见 README「快速开始」里那段「用哪个 shell 发 SQL」。
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."   # 始终在仓库根执行（compose 文件在那儿）

# 【兜底提示】这个脚本只该在「有 docker 的 shell」里跑：Git Bash / 装了 docker 的 WSL / Linux / macOS。
# Windows 的 cmd / PowerShell 里 `bash` 命中的是 C:\Windows\System32\bash.exe（WSL 2 的启动器，不是 Git 的 bash），
# 而 WSL 里常常没有 docker —— 那种情况请改用 scripts\mysql.cmd（那边不走 bash）。
if ! command -v docker >/dev/null 2>&1; then
    echo "找不到 docker。如果你是在 Windows 的 cmd / PowerShell 里跑这个脚本：" >&2
    echo "  那边的 bash 是 WSL 2 的启动器，不是 Git 的 bash，而 WSL 里通常没有 docker。" >&2
    echo "  请改用： scripts\\mysql.cmd \"select …;\"" >&2
    exit 1
fi

if [ "$#" -gt 0 ]; then
    # SQL 走【环境变量】送进容器：在宿主这边它只是一个普通参数，不必再套一层引号。
    # 下面单引号里的 $SQL 是给【容器里的 sh】看的，所以这里别展开它。
    #
    # 【为什么末尾要 </dev/null】mysql 即使带了 -e，之后**仍会继续读 stdin**；而
    # `docker compose exec -T` 会把 stdin 透进容器 —— 于是它会把"宿主这边还没执行的东西"
    # （比如你在 cmd.exe 里接着粘的下一条命令）当成 SQL 吃掉：表现是**第一条跑了、后面几条凭空消失**。
    # 显式喂 /dev/null，宿主自己的 stdin 才留得住。
    exec docker compose exec -T -e "SQL=$1" mysql \
        sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot "$MYSQL_DATABASE" -e "$SQL"' </dev/null
else
    # 没给 SQL：把 stdin 原样透传（docker compose exec -T 会把 stdin 接到容器里的 mysql）
    exec docker compose exec -T mysql \
        sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot "$MYSQL_DATABASE"'
fi
