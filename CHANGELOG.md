# 更新日志

每个版本对应仓库 Releases 页面上的一次发布，发布方法见 README 的「发布新版本」。

## v1.0.0 · 2026-10-04

第一个正式版本。每天北京时间 8 点前后，把 15 条精选内容推送到钉钉群，大约 5 分钟读完。每天的完整版永久保存在仓库里，随时可以回查。

### 信息源

共 27 个来源，分成六个板块，下面按消息里的顺序列出：

- 🌍 世界与财经：BBC 国际、Al Jazeera、美联储、CNBC 经济、经济学人财经版、FT中文网
- 🗞️ 科技热议：Hacker News、Lobsters
- 🧪 实验室动态：OpenAI、Anthropic、Google DeepMind、Meta AI、DeepSeek、Mistral AI 等 10 家
- 📄 论文：Hugging Face 每日论文、arXiv 量化金融
- 📚 期刊与科学：Nature、Nature Machine Intelligence、Science、Cell、PNAS、Quanta
- 🛠️ 开源项目：GitHub Trending

### 选内容

- 每个来源先过各自的热度或质量门槛。同一篇文章出现在多个来源时只保留一条，其他来源放进「另见」。推送过的内容 30 天内不会再出现。
- 大模型按信息量给每条打 0 到 10 分，低于 5 分的不入选；营销、招聘和活动类内容也不入选。空出的名额由替补补上。
- 报道同一事件的多条只保留一条。
- 为了保持视野，任何一个领域最多占当天的 40%，每天至少覆盖 4 个领域。AI 和金融是侧重领域，但只加权、不排除，至少三分之一名额留给其他领域。

### 消息排版

- 开头一行是条数、阅读时间和领域分布，接着是三条「今日要点」，钉钉通知栏里显示第一条。
- 每条的第一行是加粗的中文标题和领域标签，第二行是一句话摘要，第三行是一行灰色小字：🔥 热度 · 💬 讨论数 · 网站 · 来源。
- 消息末尾的「完整版与落选条目」链接到当天的完整版，里面有英文原标题、评分，以及落选条目和落选原因。
- 没有配置大模型，或者大模型接口出错时，简报照常推送，只是少了中文摘要和今日要点。

### 推送与归档

- GitHub Actions 每天北京时间 07:47 触发，同一天只推送一次，GitHub 延迟或重复触发时会自动跳过。
- 钉钉消息用加签方式发送，只在确定没有送达时才重试，避免重复推送。
- 归档永久保存在 `state` 分支的 `archive/` 文件夹里，包括：
  - 每天的完整版
  - 目录页，按月列出每天的条数、领域分布和要点
  - `items.jsonl`，逐条记录推送过的内容，方便检索和统计

### 安全

- 能读到密钥的任务没有写仓库的权限；能写仓库的任务不接触密钥，也不运行 Python 依赖。
- 日志和报错里不会出现钉钉的 token 和签名，也不会出现大模型的地址和密钥。大模型接口只接受 https 地址，不跟随跳转。
- 大模型的输出只取固定的文本字段，并且会转义。消息里的链接全部来自原始数据。
- 工作流里的 Action 按 commit SHA 固定版本，依赖按哈希值安装。

### 包含的 PR

- [#1](https://github.com/kekemao00/digest-bot/pull/1) Hacker News 抓取、筛选与钉钉简报排版
- [#2](https://github.com/kekemao00/digest-bot/pull/2) 接入全部信息源，跨来源合并与跨天去重
- [#3](https://github.com/kekemao00/digest-bot/pull/3) 大模型中文标题、一句话摘要、价值评分和今日要点
- [#4](https://github.com/kekemao00/digest-bot/pull/4) 每天定时推送、完整版归档与加固
- [#5](https://github.com/kekemao00/digest-bot/pull/5) 修复正式推送后去重状态和完整版没有保存
- [#6](https://github.com/kekemao00/digest-bot/pull/6) 排版改进：领域标签、图标化元信息、板块图标、领域分布
- [#7](https://github.com/kekemao00/digest-bot/pull/7) 扩展视野：新增世界与财经板块、领域多样性约束、事件级去重
- [#8](https://github.com/kekemao00/digest-bot/pull/8) 归档目录页和逐条清单，方便回查历史简报
- [#9](https://github.com/kekemao00/digest-bot/pull/9) 版本号、更新日志和发布流程
