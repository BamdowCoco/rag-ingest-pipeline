@echo off
rem ============================================================
rem  Run one SQL statement inside the mysql container.
rem  cmd.exe / PowerShell ONLY -- this does NOT use bash.
rem
rem  Why not `bash scripts/mysql.sh`?  In a real cmd / PowerShell
rem  window `bash` resolves to C:\Windows\System32\bash.exe, which is
rem  the WSL 2 launcher -- and WSL usually has no docker:
rem      The command 'docker' could not be found in this WSL 2 distro.
rem  So this wrapper calls docker.exe directly (it is on the Windows PATH).
rem
rem  Usage:
rem      scripts\mysql.cmd "select count(*) from document_clean;"
rem      scripts\mysql.cmd < q.sql
rem  Git Bash / WSL / macOS: use `bash scripts/mysql.sh "..."` instead
rem  (that one reads the password from inside the container).
rem
rem  Notes:
rem   - Password root123456 is this project's LOCAL DEV password; keep it
rem     in sync with docker-compose.yml. MYSQL_PWD is used instead of -p,
rem     so it stays off the command line and MySQL does not print the
rem     "Using a password ... can be insecure" warning.
rem   - `< NUL` on the first call is NOT optional: mysql keeps reading stdin
rem     even with -e, and `docker compose exec -T` forwards stdin into the
rem     container. Without it mysql would swallow the next lines you were
rem     about to run (paste three commands, only the first one runs).
rem   - Comments in this file are ASCII ON PURPOSE. cmd parses batch files
rem     using the console codepage (GBK / 936 here), while these bytes are
rem     UTF-8: a DBCS lead byte can swallow the line ending, and the rest
rem     of the line then gets executed as a command. Chinese explanation
rem     lives in README (section 0) instead.
rem ============================================================
cd /d "%~dp0.."
if not "%~1"=="" docker compose exec -T -e "MYSQL_PWD=root123456" mysql mysql -uroot rag_ingest -e "%~1" < NUL
if "%~1"=="" docker compose exec -T -e "MYSQL_PWD=root123456" mysql mysql -uroot rag_ingest
