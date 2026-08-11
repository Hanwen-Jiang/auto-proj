from __future__ import annotations

import json
from pathlib import Path


def _lines(value: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in value.splitlines() if line.strip())


# Public identities are API relay, gateway, routing, or model-hub brands. They must
# not look like unrelated local businesses or share a city/industry template.
SUPPLIER_NAMES = _lines(
    """
OmniAPI
NexusAPI
LinkAI
BridgeAPI
TransitAPI
GatewayAI
HubAPI
Synapse
RayAPI
AnyAPI
RelayOne
ModelPort
PromptHub
TokenBridge
RouteAI
LLMGateway
APIHarbor
FluxRelay
NovaGateway
OrbitAPI
VectorHub
MatrixRelay
PulseAPI
SwiftGateway
EchoAPI
VertexRelay
CloudBridge
ModelTransit
FastAPI
ExpressAPI
CoreLink
OpenRelay
PrimeGateway
MeshAPI
NodePilot
RouteNest
APIBeacon
ModelDock
TokenPort
InferenceHub
RelayWorks
PromptLane
CloudNexusAPI
AetherAPI
LoopGateway
SwitchAI
CrossLink
ModelMesh
KeyTransit
SignalAPI
AtlasRelay
MercuryAPI
VegaHub
LumenGateway
QuantaLink
AxiomAPI
ZenithRelay
ArclightHub
NimbusAPI
KiteGateway
PioneerAI
SummitAPI
CobaltRelay
HelixHub
AuroraGateway
DeltaLink
PrismAPI
CometRelay
HaloGateway
MosaicAI
TeraHub
VertexPort
StrataAPI
BeaconRelay
FusionGateway
AstraLink
HyperlaneAPI
ContinuumHub
ParallaxRelay
NeuralPort
OrbitBridge
RapidNexus
VelocityAPI
TurboRelay
FlashGateway
SprintAI
QuickBridge
StreamAPI
FlowTransit
DirectLLMHub
OpenRoute
UnifiedAPI
PolyModel
MultiLink
OneGateway
AllModelHub
UniversalRelay
MetaTransit
InfinityAPI
OmniRoute
万象API
星链网关
云桥中转
光速接口站
模型枢纽
智联API
远航网关
节点中转站
接口港
模型驿站
云端桥
极速API
一站式模型
百川网关
灵犀接口
跃迁中转
矩阵API
银河模型站
天际网关
无界接口
全域API
链路中心
模型转运站
智能路由站
云帆API
星门网关
光桥接口
中枢AI
万联模型
直达API
快线网关
迅捷中转
模型码头
接口集散地
多模态枢纽
算力桥
神经中继
云路由
模型交换站
API快车
接口通
模型直连站
云端中继
智算网关
高速模型站
全能接口
连接者API
模型汇
云接口仓
转接点AI
北极星API
灯塔网关
经纬中转
脉冲接口
向量模型站
量子网关
极光API
深空中继
轨道接口
棱镜模型
回声网关
风速API
闪电中转
跃动接口
飞梭网关
跨界API
连云中继
万维接口
通途模型站
聚合网关
RelayCN
ModelGo
APINow
FastModel
GoGateway
OneAPIHub
NextRelay
EasyGateway
QuickAPIHub
ModelPass
APIUnion
RelayCloud
HubMatrix
GateFlowAPI
ModelXpress
LLMPort
AITransit
PromptBridge
TokenHub
RouteCloud
APIJet
ModelLinker
NexusFlowAPI
OmniGateway
SwiftRelay
CloudPassAPI
DirectAPI
MeshGateway
ModelRoute
AnyModel
"""
)


def _channel_profiles(value: str) -> tuple[tuple[str, str], ...]:
    profiles: list[tuple[str, str]] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = tuple(field.strip() for field in line.split("|"))
        if len(fields) != 2 or any(not field for field in fields):
            raise RuntimeError("channel_profile_invalid")
        profiles.append(fields)
    return tuple(profiles)


# Explicit copy avoids the old mass-generated "noun + Plus/Pro/K12" fingerprint.
# Each row is: channel name | channel quality copy.
CHANNEL_PROFILES = _channel_profiles(
    """
niko-sol|稳定好用，长期维护
Plus快线|价格实在，日常稳定
FablePro|满血高速，表现稳定
天枢池|高缓存｜低延迟
不掺水5.6|高速耐跑，长期可用
PA-SOL|响应快，运行稳
codex特快|高缓存，高并发
Fast56|速度快，日常稳定
苍穹Pro|峰值表现也稳
随便跑跑|价格友好，响应不拖
HaHaSol|代码场景响应顺畅
ZApi56|稳定快速，接入省心
sol平价|低价可用，持续维护
OP-20X|高质量线路，稳定运行
mumu-sol|高缓存，响应稳定
SUB-PRO|长跑型线路，稳定耐用
Lime56|低延迟，维护勤快
星环池|峰值稳定，响应利落
K12小站|价格友好，长期可用
ProDock|并发够用，日常稳定
阿尔法线|缓存表现好，响应稳定
TinySOL|高峰稳定，持续维护
5.6直达|线路轻快，稳定在线
TeamBus|速度扎实，容量充足
Bug港|响应顺畅，长期可用
PlusOne|低价耐跑，稳定可用，定期维护
晚风API|响应利落，稳定可用
SOL快车|并发稳定，缓存表现好
PikaPro|高峰响应稳，维护及时
量子池|容量充足，日常稳定
Route56|低延迟，长时间可用
代码急送|代码响应快，运行稳定
K12-Lab|价格平稳，持续可用
Pro满格|峰值容量足，响应稳定
TeamNext|并发从容，长期维护
灯塔SOL|缓存稳定，响应不拖
Velo56|低延迟，日常耐跑
南风线|表现稳定，维护规律
bugteamGo|响应快，并发稳定
PlusHub|价格实在，容量够用
北辰一线|高峰可用，响应稳定
ProBox|满速响应，长期稳定
T12混合|缓存充足，运行稳定
轻舟5.6|低延迟，持续可用
SOL驿站|日常稳定，维护不断
TeamLane|并发稳定，响应干脆
K12专列|价格友好，线路稳定
满血Pro|高峰容量足，表现稳定
Bug直通|代码响应顺滑，长期可用
Plus平价|低价稳定，日常维护
NovaSOL|缓存命中高，响应稳定
代码走廊|并发够用，代码响应快
K12-24H|全天可用，延迟平稳
ProShift|峰值稳定，切换顺畅
TeamBase|响应稳定，维护踏实
56不排队|并发充足，响应不堵
AxisSOL|长跑稳定，容量有余
热缓存|缓存表现稳，延迟低
Bug主线|代码场景稳定，响应快
Plus副线|价格低，持续可用
SkyPro|峰值响应快，稳定在线
K12日常|日常稳定，价格合适
Team燃料|容量够用，长期稳定
SOL-ONE|响应干净，运行稳定
低价通|价格克制，线路可用
ProMax20|高负载稳定，容量充裕
K12Mini|轻量响应快，长期可用
PlusMix|并发平稳，维护及时
Team+Pro|峰值可用，响应稳定
BugTeamX|代码响应稳，并发充足
快点儿|延迟低，响应不拖
稳稳跑|长期稳定，维护规律
今天可用|当前可用，响应顺畅
慢价高速|价格友好，响应很快
小满SOL|缓存余量足，运行稳定
常开线|持续可用，峰值稳定
省心池|响应稳定，维护省心
夜班56|夜间延迟低，运行稳定
白天Pro|白天容量足，响应稳定
低调Team|表现稳定，并发够用
一杯Plus|价格合适，日常可用
K12口袋|轻量稳定，响应及时
Pro小仓|容量精简，峰值稳定
Bug备用|备用可用，切换响应快
SOL常驻|长期在线，响应稳定
不限时|全天稳定，容量平稳
快车道56|低延迟，高峰可用
Team日用|日常稳定，响应自然
Plus长跑|长时间可用，价格稳定
Pro短线|短任务响应快，峰值稳定
K12长线|长上下文稳定，容量够用
Bug混搭|代码响应稳，维护灵活
现货SOL|容量现成，响应稳定
平价Pro|价格适中，峰值可用
小额Plus|小额请求稳定，延迟低
Team仓|容量稳定，补充及时
K12仓|长期可用，容量有余
bug仓|代码响应稳定，备用充足
5.6仓|持续可用，响应稳健
混合仓|并发灵活，运行稳定
Aster56|低延迟，缓存稳定
CodeJet|代码响应快，峰值可用
ProPulse|响应敏捷，运行稳定
K12Port|接入顺畅，长期可用
TeamFlow|并发平稳，响应连续
BugRoute|代码路由稳，延迟低
PlusDock|容量够用，价格稳定
SolNexus|峰值可用，维护持续
Quick5X|响应快速，日常稳定
OpenSOL|开放时段稳定，容量充足
Nova20X|满负载可用，响应稳定
K12Wave|延迟平滑，长期稳定
TeamGrid|并发均匀，容量扎实
ProRelay|中继响应快，峰值稳定
BugLane|代码线路稳，持续可用
PlusNode|节点稳定，价格友好
SOLMesh|容量分散，响应稳定
FastK12|轻量响应快，运行稳定
TeamArc|峰值可用，延迟平稳
ProMint|高质量响应，长期稳定
BugBase|代码调用稳，容量够用
PlusRun|长跑可用，维护及时
SOLBox|响应扎实，缓存稳定
K12Loop|轮换平顺，持续可用
TeamRay|响应明快，并发稳定
ProLink|峰值链路稳，容量充裕
BugStack|代码栈响应稳，长期可用
PlusGo|价格轻，响应稳定
SolStream|持续响应顺畅，运行稳定
K12Gate|接入稳定，延迟可控
云梯56|高峰承载稳，响应及时
直连Pro|直连响应快，峰值可用
K12云仓|容量有余，长期稳定
Team中继|并发稳定，转发顺畅
Bug接口|代码接口稳，响应利落
Plus节点|价格友好，节点可用
SOL码头|容量补充快，响应稳定
模型快跑|低延迟，日常稳定
十点Pro|高峰表现稳，容量充足
零点Team|夜间可用，响应稳定
K12北线|线路稳定，延迟较低
Bug南线|代码响应快，持续可用
Plus东线|价格稳定，容量够用
SOL西线|响应平稳，维护持续
Pro中线|峰值稳定，链路可用
备用Team|备用容量足，响应及时
K12清流|响应清爽，运行稳定
Bug热路|高峰代码响应稳，并发够用
Plus冷路|低负载延迟小，长期可用
SOL回程|回程稳定，响应连续
晨间Pro|晨间容量足，峰值稳定
午后Team|午后响应稳，并发平滑
夜航K12|夜间延迟低，持续可用
Bug全天|全天代码响应稳，容量充足
Plus周末|周末稳定，价格友好
SOL工作日|工作时段可用，响应稳定
Pro低频|低频请求稳定，维护精细
Team高频|高频响应稳，并发有余
K12缓跑|延迟平稳，长跑可用
Bug急送|代码响应快，高峰稳定
返航线|长时稳定，回切响应快
代码通|代码场景可用，并发稳定
云上快跑|云端响应快，运行稳定
二号缓存|缓存稳定，延迟可控
七分满|容量留有余量，响应稳定
一路向北|线路长期可用，维护稳定
省钱快线|价格友好，低延迟
满格线路|容量充足，高峰稳定
轻量直达|轻量请求响应快，持续可用
模型班车|班次稳定，响应准时
快速响应|延迟低，日常可用
高峰备用|峰值容量足，备用稳定
日常主力|长期稳定，响应不拖
深夜专线|深夜可用，延迟平稳
早高峰|高峰响应稳，并发充足
路由零号|路由稳定，切换响应快
接口二队|接入可用，容量灵活
并发小组|并发扎实，运行稳定
缓存优先|缓存命中稳，响应快速
长跑通道|长时可用，维护持续
ByteSOL|代码响应稳，延迟低
Spark56|响应灵敏，日常稳定
CoreK12|核心线路稳，容量够用
TeamPort|并发稳定，接入顺畅
bugPilot|代码任务可用，响应快
PlusLine|价格稳定，长期维护
SolRapid|高峰响应快，运行稳定
Orbit20X|峰值容量足，持续可用
K12Prime|低延迟，质量稳定
TeamLite|轻量并发稳，响应及时
BugTurbo|代码响应快，峰值稳定
PlusBeam|线路响应直，长期可用
SOLZero|运行稳定，切换利落
ProDelta|峰值差异小，响应稳定
K12Edge|边缘线路可用，延迟低
TeamVault|储备容量足，运行稳定
BugSwitch|代码池切换快，响应稳定
PlusAxis|价格平稳，容量充裕
SOLDirect|直连响应快，长期稳定
Ready56|随时可用，响应稳定
"""
)


REFERENCE_MODEL_SOURCES = (
    "bugteam号池",
    "Plus主池，Pro备用",
    "Pro 20x号池",
    "K12 / Team混合池",
    "纯Plus，Team兜底",
    "Team + Plus混合池",
    "bugteam自建池",
    "K12账号为主",
    "纯血Pro 20x号池",
    "Plus / Pro混合池",
    "Team号池",
    "K12 + Plus号池",
    "bugteam / Plus混池",
    "Pro 20x自建号池",
    "Plus为主，K12备用",
    "Team / Pro混合池",
)

BUGTEAM_MODEL_SOURCES = (
    "bugteam号池",
    "bugteam自建池",
    "bugteam主池，Plus备用",
    "bugteam / Plus混合池",
    "bugteam / Team混合池",
    "bugteam为主，K12备用",
    "bugteam + Plus混合池",
    "bugteam主池，Team兜底",
)

PLUS_MODEL_SOURCES = (
    "Plus号池",
    "纯Plus号池",
    "Plus主池，Pro备用",
    "Plus主池，Team备用",
    "Plus为主，K12备用",
    "Plus / Pro混合池",
    "Plus + Team混合池",
    "Plus + K12混合池",
)

PRO_MODEL_SOURCES = (
    "Pro 20x号池",
    "纯Pro 20x号池",
    "Pro 20x自建池",
    "Pro 20x主池，Team备用",
    "Pro 20x主池，Plus备用",
    "Pro / Team混合池",
    "Pro / Plus混合池",
    "Pro 20x账号为主",
)

K12_MODEL_SOURCES = (
    "K12号池",
    "K12账号为主",
    "K12主池，Team备用",
    "K12主池，Plus备用",
    "K12 / Team混合池",
    "K12 + Plus混合池",
    "K12为主，bugteam备用",
    "K12 + Team + Plus混合池",
)

TEAM_MODEL_SOURCES = (
    "Team号池",
    "Team自建池",
    "Team主池，Plus备用",
    "Team主池，Pro备用",
    "Team + Plus混合池",
    "Team / Pro混合池",
    "Team + K12混合池",
    "Team为主，bugteam备用",
)

GENERIC_MODEL_SOURCES = (
    "bugteam号池",
    "Plus主池，Pro备用",
    "Pro 20x号池",
    "K12 / Team混合池",
    "纯Plus号池",
    "Team + Plus混合池",
    "bugteam自建池",
    "K12账号为主",
    "纯Pro 20x号池",
    "Plus / Pro混合池",
    "Team号池",
    "K12 + Plus混合池",
    "bugteam / Plus混合池",
    "Pro 20x自建池",
    "Plus为主，K12备用",
    "Team / Pro混合池",
)

MODEL_SOURCE_OVERRIDES = {
    "Quick5X": "K12 + Plus混合池",
    "早高峰": "Pro 20x号池",
}


def model_source_for(index: int, channel_name: str) -> str:
    if index < len(REFERENCE_MODEL_SOURCES):
        return REFERENCE_MODEL_SOURCES[index]
    if channel_name in MODEL_SOURCE_OVERRIDES:
        return MODEL_SOURCE_OVERRIDES[channel_name]
    lowered = channel_name.casefold()
    if "bug" in lowered:
        sources = BUGTEAM_MODEL_SOURCES
    elif "k12" in lowered:
        sources = K12_MODEL_SOURCES
    elif "team" in lowered:
        sources = TEAM_MODEL_SOURCES
    elif "plus" in lowered:
        sources = PLUS_MODEL_SOURCES
    elif "pro" in lowered or "20x" in lowered:
        sources = PRO_MODEL_SOURCES
    else:
        sources = GENERIC_MODEL_SOURCES
    return sources[index % len(sources)]


SUPPLIER_DESCRIPTION_STARTS = (
    "多模型 API 中转服务",
    "统一模型网关",
    "面向开发者的 API 转发站",
    "多供应源接口枢纽",
    "模型路由与中继服务",
    "统一接口接入网关",
    "多模型调用中心",
    "API 链路聚合服务",
    "模型接口交换站",
    "一站式 AI 网关",
    "多线路 API 中转平台",
    "模型请求路由枢纽",
    "通用 LLM 接入服务",
    "多模型接口集散地",
    "开发者 API 通道",
    "智能模型转接网关",
    "统一 LLM 路由入口",
    "模型能力聚合接口",
    "多源 API 调度中心",
    "高速模型中继站",
    "云端 API 转运服务",
    "通用模型连接网关",
    "开放模型接入枢纽",
    "多路 API 交换服务",
    "模型接口中转中心",
)

SUPPLIER_DESCRIPTION_ENDS = (
    "，保留基础监控。",
    "，根据可用状态调整。",
    "，不定期补充容量。",
    "，以稳定运行作为主要目标。",
    "，按实际额度安排调度。",
    "，保持独立线路与账号维护。",
    "，用于日常模型请求。",
    "，持续记录可用性变化。",
)


def generate() -> list[dict[str, object]]:
    count = 200
    if len(SUPPLIER_NAMES) != count or len(CHANNEL_PROFILES) != count:
        raise RuntimeError("template_catalog_must_have_200_entries")
    values: list[dict[str, object]] = []
    for index, (supplier_name, channel_profile) in enumerate(
        zip(SUPPLIER_NAMES, CHANNEL_PROFILES, strict=True)
    ):
        channel_name, channel_description = channel_profile
        model_description = model_source_for(index, channel_name)
        supplier_description = (
            SUPPLIER_DESCRIPTION_STARTS[index % len(SUPPLIER_DESCRIPTION_STARTS)]
            + SUPPLIER_DESCRIPTION_ENDS[index // len(SUPPLIER_DESCRIPTION_STARTS)]
        )
        values.append(
            {
                "template_id": f"merchant-{index + 1:03d}",
                "supplier_name": supplier_name,
                "supplier_description": supplier_description,
                "channel_name": channel_name,
                "channel_nickname": channel_name,
                "channel_description": channel_description,
                "model_description": model_description,
                "channel_type": 1,
            }
        )
    return values


def render_review(values: list[dict[str, object]]) -> str:
    lines = [
        "# 商户模板文案审阅表",
        "",
        "> 本表仅用于文案审阅；确认前不接入注册器、不部署。",
        "",
        "| M | 商户名称 | 商户说明 | 渠道名称 | 渠道说明 | 模型说明 |",
        "|---:|---|---|---|---|---|",
    ]
    for index, item in enumerate(values, start=1):
        fields = [
            str(index),
            str(item["supplier_name"]),
            str(item["supplier_description"]),
            str(item["channel_name"]),
            str(item["channel_description"]),
            str(item["model_description"]),
        ]
        lines.append("| " + " | ".join(field.replace("|", "\\|") for field in fields) + " |")
    return "\n".join(lines) + "\n"


def main() -> None:
    values = generate()
    destination = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "a6api_auto"
        / "data"
        / "merchant_templates.json"
    )
    destination.write_text(
        json.dumps(values, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    review_destination = (
        Path(__file__).resolve().parents[1] / "docs" / "merchant-template-copy-review.md"
    )
    review_destination.write_text(render_review(values), encoding="utf-8")


if __name__ == "__main__":
    main()
