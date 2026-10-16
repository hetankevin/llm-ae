#!/bin/sh
# Install LLM-AE and continue directly into interactive setup (macOS/Linux).
set -eu
umask 077
case "$(uname -s)" in
    Darwin|Linux) ;;
    *) echo 'LLM-AE supports macOS and Linux (including WSL).' >&2; exit 1 ;;
esac
command -v curl >/dev/null 2>&1 || { echo 'Install curl, then retry.' >&2; exit 1; }
# The whole body is a function so a piped script is parsed before setup reads stdin.
install_llm_ae() {
    task_tmp=$(mktemp -d)
    trap 'rm -rf "$task_tmp"' EXIT HUP INT TERM
    if ! command -v uv >/dev/null 2>&1; then
        if [ -x "$HOME/.local/bin/uv" ]; then
            task_uv="$HOME/.local/bin/uv"
        else
            echo 'Installing uv (Python and isolated app environments)…'
            curl -fsSL --proto '=https' --proto-redir '=https' https://astral.sh/uv/install.sh -o "$task_tmp/uv.sh"
            sh "$task_tmp/uv.sh"
            task_uv="$HOME/.local/bin/uv"
        fi
    else
        task_uv=$(command -v uv)
    fi
    "$task_uv" tool install --python 3.13 --upgrade --from \
        'llm-ae[setup] @ https://github.com/hetankevin/llm-ae/archive/refs/heads/main.zip' llm-ae
    "$task_uv" tool update-shell
    task_bin=$("$task_uv" tool dir --bin)
    PATH="$task_bin:$HOME/.local/bin:$PATH"
    export PATH
    echo 'Installed. Starting account and email setup…'
    if [ -t 0 ]; then
        "$task_bin/llm-ae" setup
    elif [ -r /dev/tty ]; then
        "$task_bin/llm-ae" setup </dev/tty
    else
        echo "Open a terminal and run: $task_bin/llm-ae setup"
        exit 1
    fi
}
install_llm_ae
