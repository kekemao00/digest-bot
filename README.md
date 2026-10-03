# digest-bot

每天定时推送一份精选资讯简报到钉钉：少而精，5 分钟内读完。运行在 GitHub Actions 上，不需要服务器。

每天北京时间 8 点前后推送，消息末尾链接到当天的完整版（含落选条目和原因）。当前进度：第四阶段（定时推送、归档、加固），之后按实际运行情况调门槛。

## 信息源

| 板块 | 来源 | 入选条件（默认） |
| --- | --- | --- |
| 世界与财经 | BBC 国际要闻、Al Jazeera、美联储公告、CNBC 经济、经济学人财经版、FT中文网 | 每家每天最多 1 条；BBC 按编辑排序取最重要的；美联储只要政策类公告 |
| 科技热议 | Hacker News、Lobsters | HN 分数 ≥ 200 或评论 ≥ 100；Lobsters 分数 ≥ 20；两边都有的合并为一条 |
| 实验室动态 | OpenAI、Anthropic、Google DeepMind、Google Research、Meta AI、Microsoft Research、Apple ML、DeepSeek、Mistral AI、Hugging Face 博客 | 新发布的文章，每家每天最多 1 条，排除客户案例 |
| 论文 | Hugging Face 每日论文、arXiv 量化金融（q-fin.TR / ST / PM） | HF 点赞 ≥ 20；arXiv 每天最多 1 条 |
| 期刊与科学 | Nature、Nature Machine Intelligence、Science、Cell、PNAS、Quanta Magazine | 期刊只取研究论文；Quanta 每天最多 1 篇 |
| 开源项目 | GitHub Trending（日榜） | 当日新增 star ≥ 300 |

没有 RSS 的博客（Anthropic、Meta AI、DeepSeek、Mistral）通过比较列表页发现新文章：第一次正式运行只记录现有文章，从第二次起才会推送新文章。推送过的内容记在仓库的 `state` 分支里，30 天内不会重复出现。

## 它怎么选内容

1. 每个信息源先过自己的门槛（见上表），30 天内推送过的内容直接跳过。
2. 命中侧重领域（默认 AI/LLM、金融、交易）的条目排序时加权，但不会排除其他内容。
3. 同一板块的多个来源交替排列，每个来源、每个板块都有条数上限，全天总数不超过 15 条。来源之间的热度不能直接比较，大模型评分高得多的条目可以排到权重更高的来源前面。
4. 配置了大模型时，有机会入选的条目（含同样多的替补）会先送审：模型给每条写中文标题和一句话，判断类型、领域和信息价值（0–10 分，只看内容，与侧重领域无关）。低于 5 分的、营销、招聘、活动类不入选，空出的名额由替补补上。侧重领域也改由模型判断，比关键词准确。
5. 链接不同但报道同一件事的条目（比如官方博客和 HN 讨论）由模型找出来合并成一条，其他来源放进“另见”。
6. 保持视野，防止信息茧房：
   - 每天至少三分之一的名额留给侧重领域之外的高分内容；
   - 单一领域最多占 40%（15 条里最多 6 条），每天至少覆盖 4 个领域；
   - 同一来源里同领域每多一条，排序分打 85 折，避免一个来源连着推同一类内容。
7. 没有达标内容的日子不推送。

门槛、配额在 [`config/sources.yaml`](config/sources.yaml)，侧重领域、关键词和领域多样性约束在 [`config/interests.yaml`](config/interests.yaml)，大模型的评分门槛、批量和超时在 [`config/llm.yaml`](config/llm.yaml)，提示词在 [`prompts/`](prompts/)。

## 配置钉钉机器人

1. 在钉钉群里添加“自定义机器人”，安全设置选 **加签**，记下 webhook 地址和以 `SEC` 开头的密钥。
2. 在仓库 Settings → Secrets and variables → Actions 里新建两个 Repository secret：

   | 名称 | 内容 |
   | --- | --- |
   | `DINGTALK_WEBHOOK` | `https://oapi.dingtalk.com/robot/send?access_token=...` |
   | `DINGTALK_SECRET` | `SEC...` |

3. 打开 Actions → “每日简报” → Run workflow。默认勾选“只生成预览”，结果在运行页面的 Summary 里；取消勾选就会真正发到群里。

## 定时推送和归档

- 每天北京时间 07:47 触发（`.github/workflows/daily.yml` 里的 cron，UTC 23:47）。GitHub 的定时任务整点最拥挤，常常延迟，所以提前十几分钟，消息通常在 8 点前后送达。改时间只需改 cron。
- 定时任务同一天只推一次：GitHub 延迟或重复触发时会跳过。手动运行（取消“只生成预览”）总会发送，内容是去掉已推送条目后剩下的。
- 每天的完整版存在 `state` 分支的 `archive/YYYY/MM-DD.md`，包括全部入选条目、大模型评分和排序靠前的落选条目及原因；钉钉消息末尾的“完整版与落选条目”就链到这里。归档永久保留，不会自动删除。
- 回查历史：[`archive/`](https://github.com/kekemao00/digest-bot/tree/state/archive) 下的 README 是目录页，按月列出每天的条数、领域分布和三条要点；`archive/items.jsonl` 每行一条推送过的内容（日期、标题、原标题、摘要、链接、来源、领域、评分），可以下载后检索或统计。GitHub 的代码搜索不覆盖 `state` 分支，要全文搜索时用这个文件。
- `state` 分支还存着去重记录（`state.json`，只保留最近 30 天），和代码历史分开。
- 钉钉配置有误、推送失败或所有信息源都抓取失败时，这次运行会在 Actions 页面标红，GitHub 也会按你的通知设置发邮件提醒。
- 公开仓库连续 60 天没有活动时，GitHub 会自动停用定时任务。每天提交到 `state` 分支的数据算作活动，正常运行时不会被停用；如果被停用了，在 Actions 页面重新启用即可。

## 配置大模型（可选）

不配也能用：消息里是英文原标题和来源自带的简介。配了以后，每条是中文标题、领域标签（🤖 AI、💹 金融等）和一句话摘要，顶部有今天各领域的条数和一段“今日要点”；原标题只放在完整版里，方便核对译文。

支持任何 OpenAI 兼容的 chat completions 接口（OpenAI、DeepSeek、通义千问、Kimi、OpenRouter、自建的 vLLM 等）。在 Repository secrets 里加三项：

| 名称 | 内容 |
| --- | --- |
| `LLM_BASE_URL` | 接口地址，写到 `/v1` 为止即可，例如 `https://api.deepseek.com/v1`；必须是 https |
| `LLM_API_KEY` | 密钥 |
| `LLM_MODEL` | 模型名，例如 `deepseek-chat` |

每天大约 3–5 次请求：送审 30 条左右（每批 12 条），外加一次今日要点。只发标题和来源自带的简介，不抓全文。配好后在 Actions 里勾选“只生成预览”运行一次，Summary 里会显示“大模型：审阅 N 条……”和中文样张。

接口出错、超时或者输出格式不对时，会重试一次；连续失败就停用，剩下的条目用原文，推送照常。不接受 `temperature`、`max_tokens` 参数的接口（如部分推理模型）会自动改用最少参数重试。

## 安全

- 密钥只存在 GitHub Secrets，代码会拒绝把消息发往 `oapi.dingtalk.com` 以外的地址，日志里不会出现 webhook 地址、token 或签名。
- 每日任务分成两步：生成和推送的任务能读到密钥，但没有写仓库的权限；保存数据的任务有写权限，但不接触任何密钥，也不运行 Python 依赖，只把数据提交到 `state` 分支。只由手动或定时触发，外部提交的 PR 拿不到任何密钥。
- 第三方 Action 按 commit SHA 固定版本，Python 依赖带哈希安装，Dependabot 每月检查更新。
- 外部内容进入消息前会转义 Markdown，链接只允许 http(s)。
- 大模型：地址必须是 https，不跟随跳转，日志里不出现地址、密钥和模型原始输出。抓来的内容在提示词里标明是数据；模型的输出只取几个文本字段，去掉其中的链接、按长度截断，再和外部内容一样转义。消息里的链接全部来自原始数据，即使网页内容试图操纵模型，也只能影响一句文字，加不了链接。

## 本地开发

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
PYTHONPATH=src python -m digest --dry-run --out preview   # 抓取真实数据，只生成预览
```

改了排版后，用 `UPDATE_SNAPSHOTS=1 python -m pytest` 更新 `tests/snapshots/` 里的样张快照，并在 PR 里检查它的变化。依赖变更后用 `uv pip compile --generate-hashes --universal --python-version 3.12 requirements.in -o requirements.txt`（`requirements-dev.in` 同理）重新锁定。

## 发布新版本

1. 改 `pyproject.toml` 里的 `version`，在 `CHANGELOG.md` 最上面写好这一版的说明（标题格式是 `## vX.Y.Z · 日期`），合并到 main。
2. 在 Actions 里运行「发布版本」。它会先跑一遍测试，再在 main 的最新提交上打 `vX.Y.Z` 标签，发布到 Releases 页面，并附上源码包和 SHA-256 校验值。

同一个版本号只能发布一次，重复运行会失败，不会覆盖已有的发布。版本历史见 [CHANGELOG.md](CHANGELOG.md)。
