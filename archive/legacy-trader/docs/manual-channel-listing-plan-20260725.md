# A6 渠道与 Listing 手工清单（2026-07-25）

> 本文件仅整理已经存在的 Session、模板和当时的 dry-run 结果，不会触发任何线上操作。
> `Session 原始字段` 与 `错误提交字段` 必须分开看：前者是每个账号原本保存的资料，后者是自动化错误覆盖后实际提交的共同配置。

## 1. 固定模型字段

当时准备上架的模型字段如下：

| 字段 | 值 |
| --- | --- |
| `own_model` | `gpt-5.6-sol` |
| `platform_model` | `gpt-5.6-sol` |
| `display_name` | `gpt-5.6-sol` |
| `charge_type` | `per_token` |
| 官方基础价格（input/output/cache read/cache write） | `5000000 / 30000000 / 500000 / 6250000` |

程序原本没有准备自然语言“模型说明”，当时拟提交的 `description` 是每个 Listing 的所有权标记
`a6l-*`。第 4 节保留这些旧值用于事故追踪，当前代码已废止该方案：模型说明必须使用逐商户
来源文案，所有权改由已绑定的 `listing_id`、`channel_id`、`own_model` 和 `platform_model` 校验。

## 2. Session / 模板原始渠道资料

这些字段来自每个 Session 的 `template_id`、`upstream_base_url` 和模板目录。`模型`为空表示该旧 Session 没保存模型清单，不代表线上旧渠道没有模型。

| Merchant | A6 用户 | 模板渠道名称 | 渠道说明 | Session `upstream_base_url` | Session 模型 |
| ---: | ---: | --- | --- | --- | --- |
| 1 | 9645 | 鹭湾066 | 厦门高并发稳定通道066 | `https://kxgbutycw0xz6cj.mashaji.xyz` | `gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.4, gpt-5.4-mini, codex-auto-review` |
| 2 | 9627 | 海岬103 | 青岛长上下文专用通道103 | `https://zubjmasp.mashaji.xyz` | `gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.4, gpt-5.4-mini, codex-auto-review` |
| 3 | 9760 | 榕信125 | 福州知识检索增强通道125 | `https://ebffexhgmaze.mashaji.xyz` | `gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.4, gpt-5.4-mini, codex-auto-review` |
| 4 | 7775 | 横琴199 | 珠海跨区域容灾通道199 | `https://ai.enodot.shop` | 空 |
| 5 | 9517 | 横琴198 | 珠海知识检索增强通道198 | `https://api.hsmiai.xyz` | 空 |
| 6 | 9727 | 云帆011 | 苏州高并发稳定通道011 | `https://wddxfp6cknmw7g.mashaji.xyz` | `gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.4, gpt-5.4-mini, codex-auto-review` |
| 7 | 8085 | 横琴200 | 珠海标准接口接入通道200 | `https://sub2api.flods.cyou` | 空 |
| 9 | 9590 | 星桥001 | 杭州低延迟文本通道001 | `https://sohnykaa6.mashaji.xyz` | `gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.4, gpt-5.4-mini, codex-auto-review` |
| 10 | 9598 | 秦岳058 | 西安长上下文专用通道058 | `https://dqztoule77o.mashaji.xyz` | `gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.4, gpt-5.4-mini, codex-auto-review` |
| 11 | 9703 | 湘沐095 | 长沙多模型兼容通道095 | `https://dqztoule77o.mashaji.xyz` | `gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.4, gpt-5.4-mini, codex-auto-review` |
| 12 | 9787 | 甬港072 | 宁波标准接口接入通道072 | `https://ah6tjs3ckv.mashaji.xyz` | `gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.4, gpt-5.4-mini, codex-auto-review` |
| 13 | 9605 | 甬港080 | 宁波知识检索增强通道080 | `https://kplouszvvky2i.mashaji.xyz` | `gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.4, gpt-5.4-mini, codex-auto-review` |
| 15 | 9591 | 泉序111 | 济南高并发稳定通道111 | `https://sohnykaa6.mashaji.xyz` | `gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.4, gpt-5.4-mini, codex-auto-review` |
| 17 | 9615 | 青岚025 | 南京知识检索增强通道025 | `https://api.hsmiai.xyz` | `gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.4, gpt-5.4-mini, codex-auto-review` |
| 18 | 9804 | 科屿081 | 合肥标准接口接入通道081 | `https://api.hsmiai.xyz` | `gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.4, gpt-5.4-mini, codex-auto-review` |
| 19 | 9812 | 横琴197 | 珠海代码辅助优选通道197 | `https://api.hsmiai.xyz` | 空 |

## 3. 错误提交字段（用于识别，不是手工填写建议）

自动化错误地把下面 15 项全部提交为共同 `base_url=https://ai.enodot.shop/v1`、共同模型 `gpt-5.6-sol`。这些绑定目前在本地标为 `retired`，对应新渠道已经下架。

| Merchant | 错误渠道名 | 错误渠道说明 | 生成渠道 ID |
| ---: | --- | --- | ---: |
| 2 | `ek5wkmrxuh` | `a6c-576ca646f43ca08b` | 1228 |
| 3 | `ex4h7uzcuv` | `a6c-bf0ffa6454aa0a15` | 1227 |
| 4 | `eqgb5qrv35` | `a6c-8183d846bbea88d8` | 1226 |
| 5 | `evoyrehh2j` | `a6c-abb1121cfa4c550b` | 1225 |
| 6 | `expdpxroye` | `a6c-bbc6fbc5d8246321` | 1224 |
| 7 | `epmdr4fefo` | `a6c-7b071e1485767e80` | 1223 |
| 9 | `e5oz672sek` | `a6c-ebb3efea445444e3` | 1222 |
| 10 | `e2qxc53e2j` | `a6c-d42e2eec9a4aa71f` | 1221 |
| 11 | `exfhexeuv3` | `a6c-b94e4b9295dfea12` | 1220 |
| 12 | `e7oi3eerf7` | `a6c-fb91b21225f9f2bc` | 1219 |
| 13 | `ec7sjrfpyl` | `a6c-17e49895f85fad7a` | 1218 |
| 15 | `eabkz5eewt` | `a6c-00559e90969b20d3` | 1217 |
| 17 | `ehioezkq3i` | `a6c-3a1c4caa1b4506ed` | 1216 |
| 18 | `er4jbv5czx` | `a6c-8f121af459b8e5dd` | 1215 |
| 19 | `e7lzi7juh4` | `a6c-faf28fa687e1c714` | 1214 |

Merchant 1 另有生成渠道 `1233 / egsdl3fgwi`，它来自更早的待审请求，不属于上述 15 次批量提交；该渠道也已下架。

## 4. 原 dry-run 动态 Listing 清单（已废止说明字段）

快照基准：

```text
model = gpt-5.6-sol
K_USD = 0.01394937213753052200884313337
margin = 0.20
conversion_factor = 0.833333333333
历史错误公式 F = (1 + delta) * P * conversion_factor
```

下表仅保留当时 dry-run 证据，不可作为当前定价输入。生产公式已经按实时读回修正为
`K_market=P/conversion_factor`、`F=(1+delta)*P`；`delta=0.5` 的最低原始面板价为
`0.80*K_pin`，公开市场价为 `0.96*K_pin`。

价格字段单位均为 A6 微额度。`Listing 说明` 是程序当时实际准备提交的所有权标记。

| M | 档位 | delta | 最终倍率 F | 面板倍率 P | input | output | cache read | cache write | Listing 说明 |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | floor | 0.500000 | 0.017074031496 | 0.013659225197 | 68297 | 409777 | 6830 | 85371 | `a6l-fb03fd021129e3c9` |
| 2 | floor | 0.466667 | 0.021190473998 | 0.017337656604 | 86689 | 520130 | 8669 | 108361 | `a6l-0db3537f96b25990` |
| 3 | floor | 0.433333 | 0.026299365113 | 0.022018078239 | 110091 | 660543 | 11010 | 137613 | `a6l-4f10f9f3f560962c` |
| 4 | floor | 0.400000 | 0.032639978012 | 0.027977124010 | 139886 | 839314 | 13989 | 174858 | `a6l-6d9200bdcd287409` |
| 5 | value | 0.366667 | 0.040509273133 | 0.035569109197 | 177846 | 1067074 | 17785 | 222307 | `a6l-49fc9a22fc3474d8` |
| 6 | value | 0.333333 | 0.050275806227 | 0.045248236916 | 226242 | 1357448 | 22625 | 282802 | `a6l-493a698ca0ca558a` |
| 7 | value | 0.300000 | 0.062396989535 | 0.057597221109 | 287987 | 1727917 | 28799 | 359983 | `a6l-51f0846416f52c1c` |
| 8 | value | 0.266667 | 0.077440514539 | 0.073364678678 | 366824 | 2200941 | 36683 | 458530 | `a6l-7df7e2d79227a710` |
| 9 | balanced | 0.233333 | 0.096110939594 | 0.093513371905 | 467567 | 2805402 | 46757 | 584459 | `a6l-952e152f5bbb9b1e` |
| 10 | balanced | 0.200000 | 0.119282687680 | 0.119282687681 | 596414 | 3578481 | 59642 | 745517 | `a6l-4491c172ac9c0831` |
| 11 | balanced | 0.166667 | 0.148040999708 | 0.152270699051 | 761354 | 4568121 | 76136 | 951692 | `a6l-a260511b558ebb94` |
| 12 | balanced | 0.133333 | 0.183732761398 | 0.194540628110 | 972704 | 5836219 | 97271 | 1215879 | `a6l-6379d4e80a9204ce` |
| 13 | quality | 0.100000 | 0.228029584220 | 0.248759546421 | 1243798 | 7462787 | 124380 | 1554748 | `a6l-80ae6971fdb5d607` |
| 14 | quality | 0.066667 | 0.283006094742 | 0.318381757091 | 1591909 | 9551453 | 159191 | 1989886 | `a6l-4ca048b6bba7f9d2` |
| 15 | quality | 0.033333 | 0.351237099060 | 0.407888375647 | 2039442 | 12236652 | 203945 | 2549303 | `a6l-0bd9507c2395488e` |
| 16 | profit_floor | 0 | 0.435918172959 | 0.523101807551 | 2615510 | 15693055 | 261551 | 3269387 | `a6l-3f5a6b0a80559523` |
| 17 | profit_floor | 0 | 0.541015325615 | 0.649218390738 | 3246092 | 19476552 | 324610 | 4057615 | `a6l-7c65b632a9f360ca` |
| 18 | profit_mid | 0 | 0.671450746279 | 0.805740895535 | 4028705 | 24172227 | 402871 | 5035881 | `a6l-60b2fbd1b611bf80` |
| 19 | ceiling | 0 | 0.833333333333 | 1.000000000000 | 5000000 | 30000000 | 500000 | 6250000 | `a6l-38563290bcb7e567` |

## 5. 当时未就绪的档位

Merchant `8 / 14 / 16` 当时状态为 `allocated`，没有 A6 用户 ID，因此上述三档只是计划值，没有对应登录账号或实际 Listing。
