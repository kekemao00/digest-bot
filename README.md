# digest-bot

每天定时推送一份精选资讯简报到钉钉：少而精，5 分钟内读完。运行在 GitHub Actions 上，不需要服务器。

当前进度：第一阶段（Hacker News + 钉钉消息排版 + 加签发送）。Lobsters、GitHub Trending、实验室博客、论文和期刊在第二阶段接入，大模型一句话摘要在第三阶段，定时推送和归档在第四阶段。

## 它怎么选内容

1. 每个信息源先过自己的门槛，例如 Hacker News 要求 24 小时内、分数 ≥ 200 或评论 ≥ 100。
2. 命中侧重领域（默认 AI/LLM、金融、交易）的条目排序时加权，但不会排除其他内容。
3. 每个板块有条数上限，全天总数不超过 15 条。
4. 每天至少三分之一的名额留给侧重领域之外的高分内容，避免只看到一个方向。
5. 没有达标内容的日子不推送。

门槛、配额在 [`config/sources.yaml`](config/sources.yaml)，侧重领域和关键词在 [`config/interests.yaml`](config/interests.yaml)。

## 配置钉钉机器人

1. 在钉钉群里添加“自定义机器人”，安全设置选 **加签**，记下 webhook 地址和以 `SEC` 开头的密钥。
2. 在仓库 Settings → Secrets and variables → Actions 里新建两个 Repository secret：

   | 名称 | 内容 |
   | --- | --- |
   | `DINGTALK_WEBHOOK` | `https://oapi.dingtalk.com/robot/send?access_token=...` |
   | `DINGTALK_SECRET` | `SEC...` |

3. 打开 Actions → “每日简报” → Run workflow。默认勾选“只生成预览”，结果在运行页面的 Summary 里；取消勾选就会真正发到群里。

## 安全

- 密钥只存在 GitHub Secrets，代码会拒绝把消息发往 `oapi.dingtalk.com` 以外的地址，日志里不会出现 webhook 地址、token 或签名。
- 工作流只有只读权限，只由手动或定时触发；外部提交的 PR 拿不到任何密钥。
- 第三方 Action 按 commit SHA 固定版本，Python 依赖带哈希安装，Dependabot 每月检查更新。
- 外部内容进入消息前会转义 Markdown，链接只允许 http(s)。

## 本地开发

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
PYTHONPATH=src python -m digest --dry-run --out preview   # 抓取真实数据，只生成预览
```

改了排版后，用 `UPDATE_SNAPSHOTS=1 python -m pytest` 更新 `tests/snapshots/` 里的样张快照，并在 PR 里检查它的变化。依赖变更后用 `uv pip compile --generate-hashes --universal --python-version 3.12 requirements.in -o requirements.txt`（`requirements-dev.in` 同理）重新锁定。
