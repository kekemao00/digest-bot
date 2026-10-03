#!/usr/bin/env bash
# 把 .state 里的去重状态（state.json）和每日完整版（archive/）提交到 state 分支。
# state 分支只放这些数据，和代码历史无关。
set -euo pipefail

: "${GH_TOKEN:?}" "${REPO:?}"
cd .state
if [ ! -d .git ]; then
  # 第一次运行：state 分支还不存在，新建一个只含数据的孤立分支
  git init -q
  git checkout -q --orphan state
fi
git config user.name "digest-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A
if git diff --cached --quiet; then
  echo "state 没有变化"
  exit 0
fi
git commit -q -m "简报数据 $(TZ=Asia/Shanghai date +%Y-%m-%d)"
# token 只在这一条命令里出现在 URL 中，不写进 git 配置
git push -q "https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git" HEAD:refs/heads/state
echo "state 已保存"
