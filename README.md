# digest-bot

每天早上把 15 条精选资讯推送到钉钉群，5 分钟读完。内容覆盖世界与财经、科技热议、AI 实验室、论文、期刊与科学、开源项目六个板块。大模型负责写中文标题和一句话摘要，按信息量筛掉噪声，并限制单一领域的占比，避免信息茧房。每天的完整版永久归档在仓库里，随时可以回查。

整个项目跑在 GitHub Actions 上，不需要服务器。fork 之后配好钉钉机器人就能用，步骤见[快速开始](#快速开始从-fork-到收到第一条消息)。版本记录见 [CHANGELOG.md](CHANGELOG.md)。

## 消息长什么样

下面是示意，实际消息里标题都是可以点开的链接：

```
每日简报 · 10月4日 周日
15 条 · 约 5 分钟｜AI 5 · 科学 3 · 金融 2 · 时政 2 · 软件 2 · 安全 1

今日要点
1. 今天最值得知道的一件事  第 3 条
2. ……
3. ……

🌍 世界与财经
1. 中文标题，点开是原文  🏛️ 时政
   一句话说清是什么、为什么值得看
   bbc.com · BBC
……

🗞️ 科技热议
4. 中文标题  🤖 AI
   一句话摘要
   🔥 812 · 💬 301 · github.com · HN
……

完整版与落选条目 · 数据截至 07:52
```

- 钉钉的通知栏里显示第一条要点，不用点开也知道今天最重要的是什么。
- 每条的灰色小字依次是热度、讨论数、网站和来源，点 💬 可以打开讨论页。
- 末尾的「完整版与落选条目」链接到当天的完整版，里面有英文原标题、大模型评分，以及没入选的条目和原因。

## 快速开始：从 fork 到收到第一条消息

大约需要 10 分钟。你需要一个 GitHub 账号，和一个可以添加机器人的钉钉群。

### 1. Fork 仓库

点仓库右上角的 **Fork**，保持勾选 **Copy the `main` branch only**（默认就是勾选的）。

`state` 分支里是本仓库自己的去重记录和归档，不需要带过去。你的仓库会在第一次正式推送时自动建立自己的 `state` 分支。

Fork 出来的仓库是公开的，归档也是公开的。如果想用私有仓库，见下面的[用私有仓库](#用私有仓库)。

### 2. 启用 Actions

GitHub 默认不在 fork 出来的仓库里运行工作流。打开你仓库的 **Actions** 页，点 **I understand my workflows, go ahead and enable them**。

如果左侧的「每日简报」旁边标着已停用（disabled），点进去再点 **Enable workflow**。

### 3. 创建钉钉机器人

1. 在要接收简报的钉钉群里，打开 群设置 → 机器人 → 添加机器人，选 **自定义**（通过 Webhook 接入）。
2. 安全设置只勾选 **加签**，复制以 `SEC` 开头的密钥。
   - 不要勾选「IP 地址（段）」：GitHub Actions 的出口 IP 不固定，会被钉钉拒绝。
   - 「自定义关键词」也不需要勾选。
3. 点完成，复制 Webhook 地址，形如 `https://oapi.dingtalk.com/robot/send?access_token=...`。

### 4. 填写 Secrets

在你的仓库里打开 Settings → Secrets and variables → Actions，点 **New repository secret**，逐个添加下面几项：

| 名称 | 是否必填 | 内容 |
| --- | --- | --- |
| `DINGTALK_WEBHOOK` | 必填 | 第 3 步复制的 Webhook 地址 |
| `DINGTALK_SECRET` | 必填 | 第 3 步复制的 `SEC` 开头的密钥 |
| `LLM_BASE_URL` | 可选 | 大模型接口地址，见[配置大模型](#配置大模型可选) |
| `LLM_API_KEY` | 可选 | 大模型的密钥 |
| `LLM_MODEL` | 可选 | 模型名 |

不配大模型也能推送，只是消息里是英文原标题和来源自带的简介，没有中文摘要、领域标签和今日要点。三项大模型配置要全部填上才会启用。

### 5. 先预览一次

打开 Actions → **每日简报** → **Run workflow**，保持勾选「只生成预览」，然后点绿色的 **Run workflow**。

大约 3 分钟后运行结束。点进这次运行，Summary 页面上依次是：
- 钉钉消息的样张
- 各信息源的抓取结果
- 可以展开的完整版，里面有落选条目和原因

预览不会发消息，也不需要钉钉配置，可以随时运行。

### 6. 正式推送

再运行一次，这次**取消勾选**「只生成预览」。

运行结束后，群里会收到第一条简报。你的仓库里会多出一个 `state` 分支，存着去重记录和当天的完整版。

之后每天北京时间 07:47 起自动运行。GitHub 自带的定时任务不保证准点，偶尔会晚几个小时；想每天 8 点前准时收到，再花 5 分钟做一下[准点推送](#准点推送推荐)的设置。

第一次运行时要注意两点：
- Anthropic、Meta AI、DeepSeek、Mistral 的博客没有 RSS。第一次正式运行只会记下现有的文章，从第二天起才会推送新文章。
- 当天已经正式推送过的话，定时任务当天不会再推。手动运行总会发送，内容是去掉已推送条目后剩下的部分。

### 没收到消息时

先打开 Actions 里最近一次「每日简报」的运行，看日志里的提示：

| 日志里的提示 | 原因和处理 |
| --- | --- |
| `缺少 DINGTALK_WEBHOOK`、`缺少 DINGTALK_SECRET` | Secret 没填或名字拼错了，名字要和上表完全一致 |
| `DINGTALK_WEBHOOK 必须是 https://oapi.dingtalk.com/ 开头的地址` | Webhook 地址没复制完整 |
| `钉钉拒绝了消息：errcode=310000 …sign not match…` | 加签密钥不对，或者机器人的安全设置不是「加签」 |
| `钉钉拒绝了消息：errcode=310000 …ip…` 或 `…keywords…` | 安全设置里勾了 IP 地址段或自定义关键词，去掉后重试 |
| `今天（…）已经推送过，定时任务不再重复推送` | 正常现象，当天已经推送过 |
| `今天没有达到门槛的内容，不推送` | 当天没有合格的内容，可以在[调整配置](#调整配置)里放宽门槛 |
| `未配置大模型…`，或者 `大模型配置有误…` | 三项大模型 Secret 没有填全，或者地址不是 https |

运行成功但群里没有消息时，检查是不是勾选了「只生成预览」。

定时任务没有按时运行时，先确认 Actions 已经启用（第 2 步）。GitHub 自带的定时任务在高峰期可能晚几个小时，甚至被丢弃，这时由 08:37 或 09:37 的备用触发补上；要准点，见[准点推送](#准点推送推荐)。另外，公开仓库连续 60 天没有活动时，GitHub 会自动停用定时任务，到 Actions 页面重新启用即可。

### 用私有仓库

Fork 只能是公开的。想用私有仓库的话，先在 GitHub 上新建一个空的私有仓库，再把本仓库的 `main` 分支推过去：

```bash
git clone --single-branch --branch main https://github.com/kekemao00/digest-bot.git
cd digest-bot
git push https://github.com/<你的用户名>/<私有仓库名>.git main
```

之后按第 3 到第 6 步操作。新建的仓库默认已经启用 Actions，不需要第 2 步。

私有仓库会占用 Actions 的免费额度。每天的运行大约计 4 分钟，一个月 120 分钟左右，在免费账号每月 2000 分钟的额度之内。

钉钉消息里的完整版链接，需要登录有权限的 GitHub 账号才能打开。

### 同步上游的更新

在你的 fork 页面上点 **Sync fork** → **Update branch**，就能拿到本仓库的新功能和修复。

如果你改过的配置文件和上游改了同一处，GitHub 会提示冲突，按页面提示处理即可。私有仓库用 `git pull https://github.com/kekemao00/digest-bot.git main` 同步。

## 调整配置

所有配置都是仓库里的文本文件。在 GitHub 网页上打开文件，点右上角的铅笔图标修改，提交到 `main` 分支，下一次运行就会生效。

改完之后，建议先用「只生成预览」运行一次看看效果。

| 想调整什么 | 改哪里 |
| --- | --- |
| 推送时间 | 用了[准点推送](#准点推送推荐)时，改 cron-job.org 上的执行时间。[`.github/workflows/daily.yml`](.github/workflows/daily.yml) 里的三行 `cron` 是 GitHub 自带的兜底触发，用 UTC 时间，等于北京时间减 8 小时，改成比准点时间稍晚即可。例如想在中午 12 点前后收到，三行依次写 `47 3 * * *`、`37 4 * * *`、`37 5 * * *` |
| 简报标题 | [`config/sources.yaml`](config/sources.yaml) 里的 `digest.title` |
| 每天的总条数 | `config/sources.yaml` 里的 `digest.max_items`，默认 15 |
| 板块的顺序、名称、图标和条数上限 | `config/sources.yaml` 里的 `sections` |
| 停用某个信息源 | 在 `config/sources.yaml` 里该来源下加一行 `enabled: false` |
| 信息源的门槛 | 各来源下的 `min_points`、`min_comments`、`min_score`、`min_upvotes`、`min_stars_today` |
| 某个来源每天最多几条、排序权重 | 该来源下的 `max_items` 和 `weight` |
| 侧重领域和关键词 | [`config/interests.yaml`](config/interests.yaml) 里的 `focus`。侧重领域只加权、不排除。不想要侧重领域时，把 `focus` 删掉 |
| 给其他领域留多少名额、单一领域最多占多少 | `config/interests.yaml` 里的 `min_outside_focus` 和 `diversity` |
| 大模型的评分门槛 | [`config/llm.yaml`](config/llm.yaml) 里的 `min_score`，默认 5，调高会更严格 |
| 今日要点的条数 | `config/llm.yaml` 里的 `highlights`，设为 0 就不显示今日要点 |
| 评分标准和摘要的写法 | [`prompts/review.md`](prompts/review.md)；今日要点的写法在 [`prompts/highlights.md`](prompts/highlights.md) |
| 完整版链接的地址 | `config/sources.yaml` 里的 `digest.archive_base_url`，默认指向你仓库 `state` 分支的 `archive/` |

### 添加一个 RSS 信息源

在 `config/sources.yaml` 的 `sources` 列表里加一段：

```yaml
  - id: my-blog              # 唯一的英文标识
    type: feed               # RSS 或 Atom 订阅源
    name: 某博客              # 消息里显示的来源名
    section: tech            # 放进哪个板块，填 sections 里的 key
    url: https://example.com/feed.xml
    window_hours: 48         # 只看最近多少小时内的文章，默认 72
    max_items: 1             # 每天最多入选几条
    weight: 0.8              # 同一板块里的排序权重，默认 1，越大越靠前
    exclude_title: "(?i)sponsored|webinar"   # 标题命中这个正则就丢弃
```

注意以下几点：
- 订阅源没有热度数据，同一来源里越新的文章越靠前。
- `sources` 里的顺序有意义：同一篇文章出现在多个来源时，保留排在前面的那个，其他的显示为「另见」。
- 其他类型（`hackernews`、`lobsters`、`page`、`crossref`、`hf_daily_papers`、`github_trending`）可以参考文件里现有的写法。没有 RSS 的网站用 `page`，它会比较列表页，只推送新出现的文章。

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

这些门槛、配额和约束都可以修改，见[调整配置](#调整配置)。

## 定时推送和归档

- 推送有两路触发。主路是外部定时服务每天 07:47 准点调用，需要设置一次，见下面的[准点推送](#准点推送推荐)。兜底是 GitHub 自带的定时任务，在 07:47、08:37、09:37 各触发一次（`.github/workflows/daily.yml` 里的三行 cron，用 UTC 时间）。
- GitHub 自带的定时任务不保证准点：负载高时会延迟，严重时直接丢弃，也不补跑。整点最拥挤，UTC 0 点（北京 8 点）前后尤其严重。2026-10-04 07:47 的触发就晚了 3 小时，10:52 才运行。
- 每天只推一次：定时触发（包括外部定时服务）到达时，如果当天已经推送过就跳过，没推过就推送，没有截止时间，所以晚到的兜底触发也会推送。手动运行（取消“只生成预览”）总会发送，内容是去掉已推送条目后剩下的。
- 每天的完整版存在 `state` 分支的 `archive/YYYY/MM-DD.md`，包括全部入选条目、大模型评分和排序靠前的落选条目及原因；钉钉消息末尾的“完整版与落选条目”就链到这里。归档永久保留，不会自动删除。
- 回查历史：`state` 分支的 `archive/` 下的 README 是目录页（本仓库的在[这里](https://github.com/kekemao00/digest-bot/tree/state/archive)），按月列出每天的条数、领域分布和三条要点；`archive/items.jsonl` 每行一条推送过的内容（日期、标题、原标题、摘要、链接、来源、领域、评分），可以下载后检索或统计。GitHub 的代码搜索不覆盖 `state` 分支，要全文搜索时用这个文件。
- `state` 分支还存着去重记录（`state.json`，只保留最近 30 天），和代码历史分开。
- Actions 页面上，每次运行的名称标明了触发方式：「准点触发」「GitHub 兜底」「预览」「手动推送」。一天里真正推送的只有先到的那一次，其余的点进去，Summary 写着「已推送过，跳过」。
- 推送成功后，去重记录和完整版要提交到 `state` 分支。这一步遇到网络错误会自动重试，避免下一路触发读不到「今天已推送」而重复推送。
- 钉钉配置有误、推送失败或所有信息源都抓取失败时，这次运行会在 Actions 页面标红，GitHub 也会按你的通知设置发邮件提醒。
- 公开仓库连续 60 天没有活动时，GitHub 会自动停用「每日简报」工作流，外部定时服务也触发不了（cron-job.org 会收到 `422` 并发邮件）。停用后在 Actions 页面重新启用即可。

### 准点推送（推荐）

用免费的 [cron-job.org](https://cron-job.org) 每天 07:47 调用 GitHub 的接口启动「每日简报」，通常 1 分钟内开始运行，8 点前送达。GitHub 自带的定时任务保留作兜底，外部服务出问题时也不会漏推。

**第 1 步：建一个只能触发 Actions 的 token**

在 GitHub 右上角头像 → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token：

| 选项 | 填什么 |
| --- | --- |
| Token name | `digest-bot 定时触发` |
| Expiration | 一年。到期前 GitHub 会发邮件提醒，换一个新 token 填回 cron-job.org 即可；过期期间由 GitHub 自带的定时任务兜底 |
| Repository access | Only select repositories，只选这个仓库 |
| Permissions | Repository permissions 里把 **Actions** 设为 **Read and write**，其他都不用加（Metadata 只读会自动带上） |

生成后复制 token，它只显示一次。这个 token 只能启动和查看本仓库的工作流，不能改代码，也读不到 Secrets。万一泄露，最多被人多触发几次推送；在同一个页面删掉它就失效了。

**第 2 步：在 cron-job.org 上建定时任务**

注册并登录后，点 **CREATE CRONJOB**：

- Title：`digest-bot 每日简报`
- URL：`https://api.github.com/repos/<你的用户名>/<仓库名>/actions/workflows/daily.yml/dispatches`
- 执行时间：每天 07:47，时区选 `Asia/Shanghai`
- 打开失败通知，接口出错时会发邮件

再到 **ADVANCED** 里设置：

- Request method：`POST`
- Headers，逐行添加：

  | Key | Value |
  | --- | --- |
  | `Accept` | `application/vnd.github+json` |
  | `Authorization` | `Bearer github_pat_…`（`Bearer`、一个空格、再接第 1 步的 token） |
  | `X-GitHub-Api-Version` | `2022-11-28` |
  | `Content-Type` | `application/json` |

  Authorization 的值最容易填错：开头的 `Bearer ` 不能省，只填 token 会返回 `401`。

- Request body：

  ```json
  {"ref": "main", "inputs": {"dry_run": "false", "scheduled": "true"}}
  ```

  `scheduled` 让这次运行和定时触发一样，当天已经推送过就跳过，所以外部服务重试或者和兜底触发撞在一起，也不会重复推送。

**第 3 步：测试**

保存后点 **TEST RUN**。返回 `204` 就是成功，Actions 页面会多出一次「每日简报 · 准点触发」。当天已经推送过的话，这次运行的 Summary 会写「已推送过，跳过」，不会重复发消息，所以白天随时可以测试。

返回其他状态码时：

| 状态码 | 原因 |
| --- | --- |
| `401` | GitHub 没认出 token。最常见的是 Authorization 的值漏了开头的 `Bearer `；其次是 token 没复制完整、首尾带了空格，或者已过期、被删除。ADVANCED 里的 HTTP authentication 要保持关闭 |
| `403` | token 没有 Actions 的写权限，回第 1 步检查 Permissions |
| `404` | URL 里的用户名或仓库名不对，或者 token 没有选这个仓库 |
| `422` | Request body 格式不对，或者「每日简报」工作流因为 60 天没有活动被停用了，见[定时推送和归档](#定时推送和归档) |

## 配置大模型（可选）

不配也能用：消息里是英文原标题和来源自带的简介。配了以后，每条是中文标题、领域标签（🤖 AI、💹 金融等）和一句话摘要，顶部有今天各领域的条数和一段“今日要点”；原标题只放在完整版里，方便核对译文。

支持任何 OpenAI 兼容的 chat completions 接口（OpenAI、DeepSeek、通义千问、Kimi、OpenRouter、自建的 vLLM 等）。在 Repository secrets 里加三项：

| 名称 | 内容 |
| --- | --- |
| `LLM_BASE_URL` | 接口地址，写到 `/v1` 为止即可，例如 `https://api.deepseek.com/v1`；必须是 https |
| `LLM_API_KEY` | 密钥 |
| `LLM_MODEL` | 模型名，例如 `deepseek-chat` |

每天大约 4–6 次请求：送审 30 条左右（每批 12 条），外加一次同事件合并和一次今日要点。只发标题和来源自带的简介，不抓全文。配好后在 Actions 里勾选“只生成预览”运行一次，Summary 里会显示“大模型：审阅 N 条……”和中文样张。

接口出错、超时或者输出格式不对时，会重试一次；连续失败就停用，剩下的条目用原文，推送照常。不接受 `temperature`、`max_tokens` 参数的接口（如部分推理模型）会自动改用最少参数重试。

## 安全

- 密钥只存在 GitHub Secrets，代码会拒绝把消息发往 `oapi.dingtalk.com` 以外的地址，日志里不会出现 webhook 地址、token 或签名。
- 每日任务分成两步：生成和推送的任务能读到密钥，但没有写仓库的权限；保存数据的任务有写权限，但不接触任何密钥，也不运行 Python 依赖，只把数据提交到 `state` 分支。只由手动、定时或外部定时服务触发，外部提交的 PR 拿不到任何密钥。外部定时服务用的 token 只有本仓库的 Actions 权限，见[准点推送](#准点推送推荐)。
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
