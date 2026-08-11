curl --url '<https://a6api.com/api/status>' \
  -H 'accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7' \
  -H 'accept-language: en,zh-CN;q=0.9,zh;q=0.8' \
  -H 'priority: u=0, i' \
  -H 'sec-ch-ua: "Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"' \
  -H 'sec-ch-ua-mobile: ?0' \
  -H 'sec-ch-ua-platform: "macOS"' \
  -H 'sec-fetch-dest: document' \
  -H 'sec-fetch-mode: navigate' \
  -H 'sec-fetch-site: none' \
  -H 'sec-fetch-user: ?1' \
  -H 'upgrade-insecure-requests: 1' \
  -H 'user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'

{
    "data": {
        "HeaderNavModules": "",
        "SidebarModulesAdmin": "{\"chat\":{\"enabled\":true,\"playground\":false,\"chat\":false},\"console\":{\"enabled\":true,\"detail\":true,\"token\":true,\"log\":true,\"midjourney\":true,\"task\":true},\"personal\":{\"enabled\":true,\"topup\":true,\"personal\":true},\"admin\":{\"enabled\":true,\"channel\":true,\"models\":true,\"deployment\":true,\"redemption\":true,\"user\":true,\"subscription\":true,\"setting\":true,\"admin-smart-routing\":true,\"admin-commission\":true,\"payment-fx\":true}}",
        "announcements": [
            {
                "content": "公告：\n\n今日平台服务器压力较大，部分请求可能出现响应变慢、失败或不稳定的情况。\n\n我们正在紧急排查并修复相关问题。为避免影响正常使用，建议大家暂时切换至其他可用线路或备用方案。\n\n修复完成后，我们会第一时间发布通知。给大家带来的不便，敬请谅解，感谢大家的支持与配合。",
                "extra": "",
                "id": 14,
                "publishDate": "2026-08-02T11:57:48.927Z",
                "type": "default"
            },
            {
                "content": "好消息：\n\nA6API 已正式入驻 CCSwitch 供应商列表。\n\n现在用户在 CCSwitch 中找到 A6API 后，只需要复制并填写自己的 A6API 密钥，即可正常调用相关模型服务，不再需要进行过多繁琐配置，接入更简单，使用更方便。\n\nA6API 平台也在持续更新中，后续会陆续带来更多功能优化、模型支持和使用体验提升。感谢大家一直以来的支持，也欢迎继续反馈建议。",
                "extra": "",
                "id": 13,
                "publishDate": "2026-07-30T08:55:28.834Z",
                "type": "default"
            },
            {
                "content": "公告：\n\n为保障平台用户体验、价格秩序和商家服务质量，A6API 将进一步加强商家风控管理。\n\n即日起，如发现商家存在以下情况，将一律列入平台风控范围：\n\n1. 商家价格突然上涨 1 倍以上，且未提前说明或存在异常涨价行为  \n2. 输入量、输出量、缓存读写量超出真实使用情况  \n3. 模型名称、模型能力、接口说明与实际服务不匹配  \n4. 存在数据掺水、虚假标注、误导用户或异常计费行为  \n5. 其他影响用户体验、平台秩序或损害用户权益的不良行为\n\n一经核实，平台将视情况采取下架、限制结算、暂停服务、封禁商户账号等处理措施。\n\n对于确认存在掺水、虚假计费、模型不匹配、异常涨价或其他恶意行为的商家，平台将直接封禁商户账号，商户账户余额一律不予退回。\n\n请所有商家立即自查价格、模型、计费、输入输出量、缓存读写量及服务说明，确保展示内容、实际调用和计费数据真实一致。\n\n平台将持续巡查并严格执行风控规则，请各位商家务必重视。",
                "extra": "",
                "id": 12,
                "publishDate": "2026-07-29T14:15:37.486Z",
                "type": "default"
            },
            {
                "content": "📣 【重要公告】技术团队正式成立暨 A6api 优化计划说明\n尊敬的开发者与用户，大家好！\n我们非常高兴地向大家宣布一个重要里程碑：我们的专属技术团队将于 8月3日 正式成立！这标志着我们的服务能力、系统稳定性以及问题响应速度都将迈上一个全新的台阶。\n在此，我们要向所有参与并陪伴我们走过内测阶段的用户，表达最诚挚的感谢。正是因为你们在早期使用过程中的包容、耐心以及提供的宝贵反馈，我们才能不断明确方向，持续打磨产品。\n针对大家在内测期间反馈的关于 A6api 的各项使用问题，随着技术团队的正式入驻，我们将启动全面且系统性的优化工作。具体计划如下：\n问题排查与修复： 团队将对内测期间收集到的 A6api 相关反馈进行逐一梳理，并陆续完成修复，解决调用过程中遇到的各类异常与痛点。\n性能与架构优化： 除了修复已知问题，我们还将深耕底层逻辑，进一步优化接口的响应延迟与并发处理能力，确保系统的极致稳定。\n持续迭代更新： 后续我们将建立更规范的更新发布机制，及时向大家同步优化进度与新功能。\n我们的核心宗旨始终未变——为您提供最稳定、最高效、最优的 API 使用体验。\n感谢大家一直以来的信任与支持！未来，我们将以更专业的技术力量，为您提供更坚实的业务保障。敬请期待我们后续的更新与蜕变！\nA6api 运营与技术团队 2026年7月",
                "extra": "",
                "id": 11,
                "publishDate": "2026-07-28T13:32:58.500Z",
                "type": "default"
            },
            {
                "content": "公告：\n\nA6API 已完成商家渠道探活与冷却机制优化。\n\n本次更新后，平台会更及时地检测商家渠道状态，并对异常、不可用或频繁失败的渠道进行冷却处理，减少用户请求到异常线路的情况，进一步提升整体调用稳定性和成功率。\n\n如大家在使用过程中仍发现渠道异常、请求失败、冷却不准确或其他问题，欢迎随时通过工单或群内联系我们反馈。\n\n感谢大家的支持与配合，我们会持续优化平台体验。",
                "extra": "",
                "id": 10,
                "publishDate": "2026-07-22T05:57:17.834Z",
                "type": "default"
            },
            {
                "content": "【公告】关于深化渠道缓存规范及违规加重处罚的通知\n各位合作伙伴/商家：\n为了维护健康、高效的 API 生态，平台即日起将加大对渠道缓存质量的监控力度。请各商家在平台检测前，先自行对旗下渠道开展全面自检。相关管理条例明确如下：\n一、 监控与下架机制 平台将对所有渠道进行常规缓存测试。对于低缓存渠道，商家须立即启动优化流程。 任何经系统检测属于低/无缓存，或被客户投诉举报的渠道，平台将触发快速下架机制，暂停其交易权限。\n二、 重新上架规范 被下架的渠道必须完成缓存优化与改良。 确认状态稳定并满足缓存要求后，方可恢复上架。\n三、 恶意违规处罚 严禁采取“零改良、直接重新提交”的恶意刷量或试探行为。 凡发现未作任何完善、重新上架已被下架渠道的，平台将视为严重恶意违规，直接采取最高级别处罚：立即封禁账户。\n良好的生态需要大家共同维护，请务必合规运营。",
                "extra": "",
                "id": 9,
                "publishDate": "2026-07-17T16:50:40.252Z",
                "type": "default"
            },
            {
                "content": "A6API API 接口地址统一说明\n\nA6API 提供两个功能一致的 API 入口，区别仅在网络连接方式：\n\nCloudflare 加速入口（默认）\nhttps://api.a6api.com\n\n直连入口（全功能兼容）\nhttps://a6.a6api.com\n\n两个入口使用相同的 API Key、模型、余额、额度、计费、路由和接口能力。一般用户建议使用 Cloudflare 入口；如果所在网络访问 Cloudflare 不稳定，或遇到图片、上传、长连接问题，可以切换到直连入口。\n\n一、OpenAI 兼容接口\n\nOpenAI SDK、Cursor、OpenCode、Continue 以及其他 OpenAI 兼容客户端，请填写：\n\n默认：\nhttps://api.a6api.com/v1\n\n直连：\nhttps://a6.a6api.com/v1\n\n常用接口：\n\nGET  {BaseURL}/models\nPOST {BaseURL}/chat/completions\nPOST {BaseURL}/responses\nPOST {BaseURL}/completions\nPOST {BaseURL}/embeddings\nPOST {BaseURL}/images/generations\nPOST {BaseURL}/images/edits\nPOST {BaseURL}/edits\nPOST {BaseURL}/audio/transcriptions\nPOST {BaseURL}/audio/translations\nPOST {BaseURL}/audio/speech\nPOST {BaseURL}/rerank\nPOST {BaseURL}/moderations\n\n例如：\n\nhttps://api.a6api.com/v1/chat/completions\nhttps://a6.a6api.com/v1/chat/completions\n\nClaude、Gemini 等模型如果通过 OpenAI 兼容格式调用，也使用上面的 `/v1` Base URL。\n\n二、Claude 原生接口\n\nClaude 原生 Messages 接口：\n\n默认：\nhttps://api.a6api.com/v1/messages\n\n直连：\nhttps://a6.a6api.com/v1/messages\n\n如果使用 Anthropic SDK 的 `base_url` 配置项，通常填写域名根地址：\n\nhttps://api.a6api.com\n\n或：\n\nhttps://a6.a6api.com\n\n最终请求应当是：\n\nhttps://api.a6api.com/v1/messages\n\n\n请根据 SDK 是否自动补充 `/v1/messages` 进行配置，避免出现：\n\n/v1/v1/messages\n/messages/v1/messages\n\n\n三、Gemini 原生接口\n\nGemini 原生协议使用 `/v1beta`：\n\nGET  https://api.a6api.com/v1beta/models\nPOST https://api.a6api.com/v1beta/models/{model}:generateContent\n\n\n直连方式：\n\nGET  https://a6.a6api.com/v1beta/models\nPOST https://a6.a6api.com/v1beta/models/{model}:generateContent\n\n\nGemini 原生接口不要改成 `/v1`。\n\n四、其他任务接口\n\n以下接口使用各自的路径，不属于 OpenAI `/v1` Base URL：\n\nMidjourney：\nhttps://api.a6api.com/mj/*\nhttps://a6.a6api.com/mj/*\n\nSuno：\nhttps://api.a6api.com/suno/*\nhttps://a6.a6api.com/suno/*\n\nKling：\nhttps://api.a6api.com/kling/v1/*\nhttps://a6.a6api.com/kling/v1/*\n\n即梦：\nhttps://api.a6api.com/jimeng\nhttps://a6.a6api.com/jimeng\n\n\n实时 WebSocket：\n\nwss://api.a6api.com/v1/realtime\nwss://a6.a6api.com/v1/realtime\n\n\n五、请注意\n\n1. OpenAI SDK 的 Base URL 只填写到 `/v1`，不要填写完整的 `/chat/completions` 或 `/responses`。\n2. 不要把 Claude 的 `/v1/messages` 填入会自动拼接路径的 SDK `base_url`。\n3. 不要把 Gemini 原生接口改成 `/v1`。\n4. `https://a6api.com` 主要用于官网和控制台，不作为新的 OpenAI Base URL。\n5. 两个 API 入口不需要分别创建 API Key。\n6. 不建议使用 `www.a6api.com`、`api-direct.a6api.com` 或其他备用地址作为新项目入口。\n\n六、当前未支持的接口\n\n以下接口目前不能因为更换域名而恢复：\n\n/v1/messages/count_tokens\n/v1/files/*\n/v1/fine-tunes/*\n/v1/images/variations\n\n\n以上接口是否支持，以平台最新开发文档为准。\n\n**最简单的记忆方式：**\n\n\nOpenAI 兼容：域名 + /v1\nClaude 原生：域名 + /v1/messages\nGemini 原生：域名 + /v1beta\n任务类接口：域名 + 对应任务路径",
                "extra": "",
                "id": 8,
                "publishDate": "2026-07-17T08:58:48.371Z",
                "type": "default"
            },
            {
                "content": "公告：\n\nA6API 已上线个人智能路由增强功能。\n\n本次更新后，用户可以按“令牌 + 模型”单独配置自己的智能路由池，支持手动添加指定商家，也支持按模型市场页面实时倍率自动入池。手动池和自动池会合并后参与路由，方便用户按自己的价格范围和商家偏好进行选择。\n\n当前请求路由顺序为：\n\n固定商家 -\u003e 个人智能路由池 -\u003e 平台智能优选 -\u003e 系统默认路由\n\n简单说明：\n\n- 如果已固定商家，系统会优先尝试固定商家\n- 未固定时，会进入用户自己配置的个人智能路由池\n- 个人智能路由池包含手动加入商家和按实时倍率自动入池商家\n- 当个人智能路由池没有可用商家，且用户开启“允许兜底”时，才会继续进入平台智能优选\n- 如果平台智能优选仍不可用，最后才进入系统默认路由\n\n简单来说，如果你想完全自己控制商家选择，可以配置个人智能池并关闭兜底；如果你想优先使用自己的池子，失败后再由平台补位，可以开启允许兜底。\n\n本次更新让用户可以更灵活地控制路由策略，同时兼顾价格、稳定性和请求成功率。",
                "extra": "",
                "id": 7,
                "publishDate": "2026-07-05T14:49:20.091Z",
                "type": "default"
            },
            {
                "content": "公告：\n\nA6API 已更新商家线路冷却规则。\n\n当某个商家线路出现频繁请求失败、超时、异常报错等情况时，系统会自动将该线路暂时加入冷却状态，减少用户继续请求到异常线路的概率。\n\n冷却期间，智能路由会优先选择其他可用线路，尽量提升请求成功率和整体稳定性。后续我们也会持续根据实际使用情况优化规则。\n\n如遇到请求失败或模型异常，欢迎及时反馈，我们会持续排查和更新。",
                "extra": "",
                "id": 6,
                "publishDate": "2026-07-04T15:22:43.746Z",
                "type": "default"
            },
            {
                "content": "公告：\n\nA6API 已正式开始推广，平台流量将持续增长。\n\n如果你有优质模型资源、稳定 API 货源、低价高质量上游渠道，欢迎加入我们成为供货商。平台支持商家入驻、多模型供给、按量结算和流量共享，优质资源可获得更多曝光与订单机会。\n\n我们希望与稳定、可靠、有价格优势的上游伙伴长期合作，一起为用户提供更低价、更稳定、更丰富的 AI 模型服务。\n\n有优质货源的朋友，欢迎联系我们入驻合作。",
                "extra": "",
                "id": 5,
                "publishDate": "2026-07-01T16:35:26.465Z",
                "type": "default"
            },
            {
                "content": "公告：\n\n为进一步提升服务体验，我们已开通 24 小时客服支持，大家可通过 Telegram 群或平台工单两种方式联系我们。\n\n如在使用过程中遇到充值、调用、模型、商户入驻、渠道合作或其他相关问题，均可随时提交工单或在群内反馈。\n\n我们会尽快响应并协助处理，感谢大家一直以来的支持。\n\nTG群：https://t.me/a6apicom",
                "extra": "",
                "id": 4,
                "publishDate": "2026-07-01T10:32:36.296Z",
                "type": "default"
            },
            {
                "content": "公告：\n\n我们正在更新 Anthropic 智能路由功能，升级期间建议暂时不要使用 Anthropic 相关模型，以免影响请求稳定性。\n\n其他模型和功能不受影响，可正常使用。\n\n本次升级预计需要 1-2 天完成。升级完成后，我们会再次发布通知，请大家留意后续公告。",
                "extra": "",
                "id": 3,
                "publishDate": "2026-06-30T05:37:03.819Z",
                "type": "default"
            },
            {
                "content": "关于异常高频 API 调用的风控提醒\n为保障平台稳定运行和所有用户的正常使用体验，A6API 会持续监控异常调用行为。\n请勿通过脚本、自动化程序或拆分请求的方式，进行大量小额、高频、重复性的 API 调用。包括但不限于：短时间内密集发起非正常频率请求、刻意拆分任务形成小额高频调用、持续占用高价值模型资源、影响平台服务稳定或其他用户正常使用的行为。\n如系统识别到上述异常行为，平台将视情况采取限速、暂停 API Key、限制账户调用、冻结账户或封禁账户等处理。情节严重或多次触发风控的账户，将不再另行通知。\n正常开发、测试和业务调用不受影响。请合理控制并发和请求频率，优先使用合适的调用方式，避免造成异常请求特征。",
                "extra": "",
                "id": 2,
                "publishDate": "2026-06-24T02:46:52.980Z",
                "type": "default"
            },
            {
                "content": "关于模型调用方式：智能优选与固定商家的区别\n亲爱的 A6API 用户：\n为了让大家更稳定、更低成本地调用 AI 模型，A6API 目前支持两种主要调用方式：智能优选 和 固定商家。\n一、推荐新用户优先使用「智能优选」\n智能优选是系统默认推荐的调用方式。\n当你调用某个模型时，系统会根据当前可用商家、价格、成功率、响应速度和线路状态，自动选择更合适的渠道。\n简单理解就是：\n你只需要选择模型，系统会自动帮你找更合适的商家。\n智能优选适合大多数用户，尤其适合：\n新用户\n不熟悉各个商家稳定性的用户\n希望更稳定调用模型的用户\n不想频繁手动切换商家的用户\n希望在低价和稳定之间自动平衡的用户\n如果某个商家临时限流、繁忙或不可用，智能优选会尽量自动切换到其他可用商家，减少请求失败。\n二、什么是「固定商家」？\n固定商家是指你手动指定某一个商家来调用模型。\n例如你选择了某个模型下的 VOVP、PANI、COSTI 等商家，那么后续请求会优先固定走这个商家。\n固定商家适合：\n你已经测试过某个商家，确认它适合你的业务\n你希望稳定使用某个指定商家的价格或线路\n你对模型效果、速度、价格有明确偏好\n你愿意自行观察该商家的状态和成功率\n需要注意：\n固定商家不代表永远可用。商家上游可能会遇到限流、余额不足、线路异常、模型维护或临时不可用等情况。\n如果你固定的商家暂时不可用，而没有开启“失败后切换智能优选”，请求可能会直接失败。\n三、固定商家失败时怎么办？\n如果你看到类似提示：\n“固定商家当前不可用”\n“该商家渠道正处于熔断冷却中”\n“上游限流或容量不足”\n“请切换商家或开启智能优选”\n说明你当前固定的商家临时不可用。\n你可以选择以下任一方式解决：\n切换到其他商家\n开启“固定商家失败后切换智能优选”\n直接使用默认智能优选\n等待该商家恢复后再使用\n四、我们的建议\n如果你是新用户，或者不确定该选哪个商家，建议直接使用：\n默认智能优选\n这样系统会自动帮你选择当前更合适的渠道，稳定性通常会更好。\n如果你是高级用户，已经明确知道自己想走哪个商家，可以使用固定商家。但建议同时开启：\n固定商家失败后切换智能优选\n这样即使指定商家临时异常，也能尽量自动切换，减少失败。\n感谢大家对 A6API 的支持。我们会持续优化模型市场、价格比价和智能调度能力，让大家用更低成本、更稳定的方式调用 AI 模型。",
                "extra": "",
                "id": 1,
                "publishDate": "2026-06-23T07:21:24Z",
                "type": "default"
            }
        ],
        "announcements_enabled": true,
        "api_info": [
            {
                "color": "blue",
                "description": "做图推荐 · 直连源站",
                "id": 2,
                "route": "香港线路",
                "url": "https://a6.a6api.com"
            },
            {
                "color": "blue",
                "description": "通用 API · Cloudflare 加速",
                "id": 3,
                "route": "香港线路",
                "url": "https://api.a6api.com"
            }
        ],
        "api_info_enabled": true,
        "chats": [
            {
                "Cherry Studio": "cherrystudio://providers/api-keys?v=1\u0026data={cherryConfig}"
            },
            {
                "AionUI": "aionui://provider/add?v=1\u0026data={aionuiConfig}"
            },
            {
                "流畅阅读": "fluentread"
            },
            {
                "CC Switch": "ccswitch"
            },
            {
                "Lobe Chat 官方示例": "https://chat-preview.lobehub.com/?settings={\"keyVaults\":{\"openai\":{\"apiKey\":\"{key}\",\"baseURL\":\"{address}/v1\"}}}"
            },
            {
                "AI as Workspace": "https://aiaw.app/set-provider?provider={\"type\":\"openai\",\"settings\":{\"apiKey\":\"{key}\",\"baseURL\":\"{address}/v1\",\"compatibility\":\"strict\"}}"
            },
            {
                "AMA 问天": "ama://set-api-key?server={address}\u0026key={key}"
            },
            {
                "OpenCat": "opencat://team/join?domain={address}\u0026token={key}"
            }
        ],
        "checkin_enabled": false,
        "custom_currency_exchange_rate": 1,
        "custom_currency_symbol": "¤",
        "data_export_default_time": "hour",
        "default_collapse_sidebar": false,
        "default_use_auto_group": true,
        "demo_site_enabled": false,
        "discord_client_id": "",
        "discord_oauth": false,
        "display_in_currency": true,
        "docs_link": "<https://docs.newapi.pro>",
        "email_verification": true,
        "enable_batch_update": false,
        "enable_data_export": true,
        "enable_drawing": true,
        "enable_task": true,
        "faq": [
            {
                "answer": "答： 可以对照以下常见问题进行排查：\n\n问题 1：设置了个人路由，实际却没走指定线路？\n\n排查： 先检查令牌和模型是否对应正确；其次确认指定的商家近期是否可用；如果开启了系统兜底，当你的路由池全挂时，系统会自动切走。\n\n问题 2：为什么请求直接报错了？\n\n排查： 通常是因为你设置的路由池线路全部不可用了，同时你又【关闭】了系统兜底。解决办法是多添加几条备用商家，或直接开启系统兜底。\n\n问题 3：配置多个商家有什么好处？\n\n排查： 能大幅降低单一商家故障导致业务中断的风险，也给系统留出在优质线路中进行比对和轮询的空间。",
                "id": 1,
                "question": "问：在使用个人路由的过程中，遇到没走指定线路或直接报错该怎么办？"
            },
            {
                "answer": "答： 个人路由是按照**“令牌 + 模型”**单独生效的。\n\n例如：如果你为“A 令牌”设置了 Claude 的个人路由，这只会影响 A 令牌调用 Claude，并不会影响 A 令牌调用其他模型，也不会影响其他令牌的调用。\n\n总结： 如果你有多个令牌或多个模型需要指定线路，必须分别进行配置。",
                "id": 2,
                "question": "问：个人路由是怎么生效的？它会影响我所有的令牌和模型吗？"
            },
            {
                "answer": "答： 系统会严格按照以下顺序进行匹配，只要前一步匹配成功且可用，就不会进入下一步：\n\n固定商家：如果你明确指定了某个商家，系统会绝对优先使用它。\n\n个人智能路由池：未指定固定商家，或固定商家不可用时，系统会从你设置的个人路由池中选择可用线路。\n\n平台智能优选：当个人路由池没有可用线路，且你开启了系统兜底时，系统会自动选择平台可用的优选线路。\n\n系统默认路由：当没有其他任何可用匹配方式时，系统将按默认规则处理请求。",
                "id": 3,
                "question": "问：当发送一个请求时，系统是按照什么顺序来匹配和选择线路的？"
            },
            {
                "answer": "答： 进入“个人路由设置”后，依次进行以下操作：\n\n选择需要配置的令牌；\n\n选择需要配置的模型；\n\n选择需要使用的商家或线路；\n\n点击保存设置。\n\n💡 建议： 首次使用时，建议先只添加 1 至 2 条稳定线路进行测试，确认调用正常后，再逐步增加其他备选线路。",
                "id": 4,
                "question": "问：我想添加一条个人路由，具体的操作步骤是什么？首次配置有什么建议吗？"
            },
            {
                "answer": "答： 这取决于你对线路掌控度的要求：\n\n方式一：手动选择商家\n\n特点： 你自己挑选并添加信任的商家，系统只在这些已选商家中按规则调度。\n\n适用场景： 适合对线路来源有明确要求、有测试过更适合自己业务的特定商家、且不希望系统自作主张的用户。\n\n方式二：自动筛选商家\n\n特点： 系统根据实时倍率、线路状态、成功率、延迟等数据，在符合条件的商家中自动帮你筛出更优线路。\n\n适用场景： 适合想优先控制成本、不想费时手动维护多个商家，希望兼顾价格和稳定性的用户。",
                "id": 5,
                "question": "问：配置方式里的“手动选择商家”和“自动筛选商家”有什么区别？我该选哪个？"
            },
            {
                "answer": "答： 这是个人路由中最重要的设置，决定了你指定的线路故障时系统如何处理：\n\n开启系统兜底： 当你设置的个人路由池全部不可用时，系统会自动切换到平台其他可用线路，优先保证调用成功，避免业务中断。（注意：兜底使用的商家和价格倍率可能与你原设定的不同）。\n\n关闭系统兜底： 当你设置的个人路由池全部不可用时，系统会直接返回错误，绝不使用平台其他线路。适合对成本、线路来源或合规有极严格要求的场景。（注意：关闭后务必多配置几条备用线路，否则极易因单点故障导致请求失败）。",
                "id": 6,
                "question": "问：什么是“系统兜底”？开启和关闭分别有什么影响？"
            },
            {
                "answer": "答： 针对四种常见需求，推荐配置如下：\n\n追求稳定、不想报错 ➔ 选择【自动筛选商家】+ 添加【多个可用商家】+ 【开启】系统兜底。\n\n追求低价、允许少量波动 ➔ 选择【自动筛选商家】+ 根据倍率设置筛选条件 + 【开启】系统兜底（防止低价线路挂掉时报错）。\n\n只想使用特定商家 ➔ 【手动添加】指定商家 + 添加 2 条以上备选线路 + 【关闭】系统兜底。\n\n业务绝不能接受自动换线路 ➔ 【手动选择】商家 + 提前确认额度与稳定性 + 【关闭】系统兜底。",
                "id": 7,
                "question": "问：针对不同的业务需求（如追求稳定、追求低价、指定商家等），有什么推荐的配置组合吗？"
            },
            {
                "answer": "答： 建议重点查看以下 5 个核心信息，切忌只看价格：\n\n倍率/价格：直接关系到你的调用成本，越低越省钱。\n\n成功率：核心指标，成功率越高说明线路越稳定。\n\n延迟：延迟越低，模型的响应速度就越快。\n\n最近成功时间：用于判断该线路近期是否活跃、是否依然存活。\n\n线路状态：若显示“异常”、“冷却”或“不可用”，绝对不要选作唯一线路。",
                "id": 8,
                "question": "问：在查看和挑选商家线路时，我应该重点关注哪些指标？"
            },
            {
                "answer": "答：别慌！这是由于大家配置的 API 入口地址不一致踩了坑。请速把你的 API 地址统一改成这个最稳的官方标准入口：\n👉 [https://a6api.com/v1](https://a6api.com/v1)\n\n避坑指南（请自查你的配置）：\n\n❌ 别用不带 /v1 的地址（如 a6api.com）：SDK 很容易把它拼错，直接撞进官网网页，导致你的程序塞满了一堆网页代码（HTML），引发解析失败。",
                "id": 9,
                "question": "问：调用 A6API 总是莫名其妙报错？一会儿返回 HTML，一会儿接口断流，怎么解？"
            }
        ],
        "faq_enabled": true,
        "footer_html": "",
        "github_client_id": "Ov23ligFZ3T5H8HuvWO9",
        "github_oauth": true,
        "linuxdo_client_id": "",
        "linuxdo_minimum_trust_level": 0,
        "linuxdo_oauth": false,
        "logo": "<https://a6api.com/brand/a6-logo-large-transparent-512.png>",
        "mj_notify_enabled": false,
        "oidc_authorization_endpoint": "<https://accounts.google.com/o/oauth2/v2/auth>",
        "oidc_client_id": "121583037146-1ebhq78s3f6slabcul19e40vmacb1rcu.apps.googleusercontent.com",
        "oidc_enabled": true,
        "passkey_allow_insecure": false,
        "passkey_attachment": "",
        "passkey_display_name": "A6API",
        "passkey_login": false,
        "passkey_origins": "<https://a6api.com>",
        "passkey_rp_id": "www.a6api.com",
        "passkey_user_verification": "preferred",
        "payment_fx_rate_cny_per_usd": "6.7444",
        "price": 6.94,
        "privacy_policy_enabled": false,
        "quota_display_type": "USD",
        "quota_per_unit": 500000,
        "self_use_mode_enabled": false,
        "server_address": "<https://a6api.com>",
        "setup": true,
        "start_time": 1786415221,
        "stripe_unit_price": 1,
        "system_name": "A6api-Token聚合站",
        "telegram_bot_id": "8728635504",
        "telegram_bot_name": "A6apibot",
        "telegram_oauth": true,
        "theme": "default",
        "turnstile_check": false,
        "turnstile_site_key": "",
        "uptime_kuma_enabled": true,
        "usd_exchange_rate": 7.3,
        "user_agreement_enabled": false,
        "version": "pr5-stream-load-10e62f54",
        "wechat_login": false,
        "wechat_qrcode": ""
    },
    "message": "",
    "success": true
}
