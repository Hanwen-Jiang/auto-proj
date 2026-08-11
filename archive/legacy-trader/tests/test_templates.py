import re
from dataclasses import replace

import pytest

from a6api_auto.errors import A6AutoError
from a6api_auto.templates import (
    choose_account_template,
    choose_template,
    load_templates,
    load_workbook_template_catalog,
    sample_workbook_template_ids,
    validate_registration_template,
)
from a6api_auto.trade_managed_listings import is_source_only_listing_description


def test_catalog_has_200_unique_realistic_templates() -> None:
    templates = load_templates()

    assert len(templates) == 200
    for field in (
        "template_id",
        "supplier_name",
        "supplier_description",
        "channel_name",
        "channel_description",
    ):
        values = [getattr(item, field) for item in templates]
        assert len(set(values)) == 200
    assert all(1 <= len(item.channel_nickname) <= 10 for item in templates)
    assert all(item.channel_type == 1 for item in templates)


def test_workbook_catalog_is_strict_and_resolves_both_independent_rows() -> None:
    catalog = load_workbook_template_catalog()

    assert catalog.sha256 == (
        "908f922bfd50cac26f7addb4bdb14fd23c42761002988ea71f1bb71d1ff1a078"
    )
    assert len(catalog.suppliers) == 1500
    assert len(catalog.channels) == 1500
    assert catalog.suppliers[0].supplier_name == "Room Indigo East"
    assert catalog.channels[0].channel_name == "一半热度"

    template = choose_template("workbook-s0002-c0003")
    assert template.supplier_name == "Room Indigo East"
    assert template.channel_name == "Pencil for Latency"
    assert template.channel_nickname == "Pencil for"
    assert validate_registration_template(template) == template


def test_workbook_sample_uses_unique_unrelated_supplier_and_channel_rows() -> None:
    template_ids = sample_workbook_template_ids(5)
    templates = [choose_template(template_id) for template_id in template_ids]

    assert len(set(template_ids)) == 5
    assert len({item.supplier_name for item in templates}) == 5
    assert len({item.channel_name for item in templates}) == 5
    assert len({item.channel_nickname for item in templates}) == 5
    assert all("重点组" not in repr(item) for item in templates)
    assert all("_" not in item.channel_name for item in templates)
    source_families = []
    for item in templates:
        lowered = item.model_description.casefold()
        without_bugteam = lowered.replace("bugteam", "")
        families = {
            family
            for family in ("pro", "plus", "k12", "team")
            if family in without_bugteam
        }
        if "bugteam" in lowered:
            families.add("bugteam")
        assert len(families) == 1
        source_families.extend(families)
    assert len(set(source_families)) == 5
    seen_name_terms: set[str] = set()
    seen_description_terms: set[str] = set()
    for item in templates:
        names = " ".join((item.supplier_name, item.channel_name)).casefold()
        ascii_terms = set(re.findall(r"[a-z0-9]{3,}", names))
        chinese = "".join(re.findall(r"[\u3400-\u9fff]", names))
        name_terms = ascii_terms | {
            chinese[index : index + 2] for index in range(max(0, len(chinese) - 1))
        }
        description_terms: set[str] = set()
        for description in (item.supplier_description, item.channel_description):
            compact = "".join(
                re.findall(r"[a-z0-9\u3400-\u9fff]", description.casefold())
            )
            description_terms.update(
                compact[index : index + 6]
                for index in range(max(0, len(compact) - 5))
            )
        assert seen_name_terms.isdisjoint(name_terms)
        assert seen_description_terms.isdisjoint(description_terms)
        seen_name_terms.update(name_terms)
        seen_description_terms.update(description_terms)


def test_ten_workbook_profiles_have_natural_names_and_unique_model_descriptions() -> None:
    template_ids = sample_workbook_template_ids(10)
    templates = [choose_template(template_id) for template_id in template_ids]

    assert all(
        not any(character.isdigit() for character in item.supplier_name)
        for item in templates
    )
    assert all(
        not any(character.isdigit() for character in item.channel_name)
        for item in templates
    )
    assert len({item.model_description for item in templates}) == 10
    assert len({item.supplier_name for item in templates}) == 10
    assert len({item.channel_name for item in templates}) == 10


def test_workbook_template_rejects_content_changed_after_selection() -> None:
    template = choose_template("workbook-s0002-c0003")

    with pytest.raises(A6AutoError, match="workbook_template_content_mismatch"):
        validate_registration_template(
            replace(template, supplier_description="changed outside workbook")
        )


def test_catalog_has_no_legacy_mass_generated_identity_fingerprints() -> None:
    templates = load_templates()
    public_text = "\n".join(
        value
        for item in templates
        for value in (
            item.supplier_name,
            item.supplier_description,
            item.channel_name,
            item.channel_description,
            item.model_description,
        )
    )
    forbidden = (
        "服务中心",
        "法律咨询",
        "医疗信息",
        "农业服务服务",
        "本地团队",
        "鹭湾",
        "横琴",
        "甬港",
        "榕信",
    )
    assert not any(value in public_text for value in forbidden)
    assert len({item.supplier_name[:3].casefold() for item in templates}) >= 150
    assert (
        sum(
            any(character.isascii() and character.isalpha() for character in item.supplier_name)
            for item in templates
        )
        >= 140
    )
    assert sum(not item.supplier_name.isascii() for item in templates) >= 70
    assert all(not item.channel_name[-3:].isdigit() for item in templates)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("supplier_name", "赣新156"),
        ("channel_description", "南昌低延迟文本通道156"),
        ("supplier_name", "厦门鹭湾法律咨询服务中心"),
    ),
)
def test_registration_gate_rejects_legacy_correlated_names(field: str, value: str) -> None:
    template = replace(load_templates()[0], **{field: value})

    with pytest.raises(A6AutoError, match="^batch_template_naming_rejected$"):
        validate_registration_template(template)


def test_supplier_names_and_descriptions_are_api_relay_brands() -> None:
    templates = load_templates()
    relay_markers = (
        "api",
        "ai",
        "relay",
        "gateway",
        "hub",
        "link",
        "route",
        "model",
        "port",
        "bridge",
        "transit",
        "node",
        "mesh",
        "lane",
        "synapse",
        "接口",
        "模型",
        "网关",
        "中转",
        "枢纽",
        "中继",
        "路由",
        "桥",
        "链",
        "节点",
        "驿站",
    )

    assert all(
        any(marker in item.supplier_name.casefold() for marker in relay_markers)
        for item in templates
    )
    assert all(
        "API" in item.supplier_description
        or "AI" in item.supplier_description
        or "模型" in item.supplier_description
        or "LLM" in item.supplier_description
        or "接口" in item.supplier_description
        for item in templates
    )


def test_channel_descriptions_only_claim_stable_service_quality() -> None:
    templates = load_templates()
    quality_markers = ("稳", "可用", "延迟", "并发", "容量", "响应")

    assert all(
        any(marker in item.channel_description for marker in quality_markers) for item in templates
    )
    assert all(
        label not in item.channel_description
        for item in templates
        for label in ("Plus", "Pro", "Team", "K12", "BugTeam")
    )


def test_model_descriptions_only_describe_account_sources() -> None:
    templates = load_templates()
    contamination = (
        "早高峰",
        "夜间",
        "日间",
        "晨间",
        "午后",
        "周末",
        "工作日",
        "路由",
        "中继",
        "接口",
        "节点",
        "直连",
        "回程",
        "东向",
        "西向",
        "北向",
        "南向",
        "高频",
        "低频",
        "快速",
        "高缓存",
        "低价",
        "平价",
        "现货",
        "云端",
        "边缘",
        "代码",
    )

    assert all(is_source_only_listing_description(item.model_description) for item in templates)
    assert all(claim not in item.model_description for item in templates for claim in contamination)


def test_manual_accounts_choose_a_stable_catalog_template_by_a6_user_id() -> None:
    assert choose_account_template("", a6_user_id=10_660).template_id == "merchant-060"
    assert choose_account_template("", a6_user_id=10_930).template_id == "merchant-130"
    assert (
        choose_account_template("merchant-026", a6_user_id=10_660).template_id
        == "merchant-026"
    )

    with pytest.raises(A6AutoError, match="template_account_identity_invalid"):
        choose_account_template("", a6_user_id=0)
