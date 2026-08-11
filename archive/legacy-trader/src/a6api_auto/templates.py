from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from .channel_policy import is_forbidden_channel_name
from .errors import A6AutoError
from .models import MerchantTemplate
from .xlsx_catalog import XlsxRow, read_xlsx_sheet

WORKBOOK_TEMPLATE_COUNT = 1500
WORKBOOK_SUPPLIER_SHEET = "商户资料"
WORKBOOK_CHANNEL_SHEET = "渠道资料"
WORKBOOK_SUPPLIER_HEADERS = ("supplier_name", "supplier_description")
WORKBOOK_CHANNEL_HEADERS = (
    "channel_name",
    "channel_description",
    "model_description",
)
WORKBOOK_TEMPLATE_ID = re.compile(r"^workbook-s(?P<supplier>[0-9]{4})-c(?P<channel>[0-9]{4})$")
_PUBLIC_GROUP_MARKERS = ("重点组",)
_MECHANICAL_CHANNEL_NAME = re.compile(
    r"(?:_|\d{2,}$|^(?:plus|pro|team|k12|bugteam|sol)(?:[-_]|(?=[\u3400-\u9fff]))|"
    r"(?:[-_])(?:plus|pro|team|k12|bugteam|sol)$)",
    re.IGNORECASE,
)
_GENERIC_IDENTITY_TERMS = frozenset(
    {
        "api",
        "ai",
        "east",
        "west",
        "north",
        "south",
        "模型",
        "服务",
        "接口",
        "文本",
        "调用",
        "工具",
    }
)
_MODEL_SOURCE_FAMILIES = ("pro", "plus", "k12", "team", "bugteam")

_RELAY_NAME_MARKERS = (
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
_FORBIDDEN_REGISTRATION_TEXT = (
    "服务中心",
    "法律",
    "律务",
    "咨询",
    "文化传媒",
    "教育科技",
    "旅游运营",
    "医疗信息",
    "金融研究",
    "跨境电商",
)
_NUMBERED_CHINESE_NAME = re.compile(r"^[\u3400-\u9fff]{2,10}\d{3}$")
_NUMBERED_CHANNEL = re.compile(r"通道\s*\d{3}(?:\D|$)", re.IGNORECASE)


@dataclass(frozen=True)
class SupplierWorkbookRow:
    source_row: int
    supplier_name: str
    supplier_description: str


@dataclass(frozen=True)
class ChannelWorkbookRow:
    source_row: int
    channel_name: str
    channel_description: str
    model_description: str


@dataclass(frozen=True)
class WorkbookTemplateCatalog:
    sha256: str
    suppliers: tuple[SupplierWorkbookRow, ...]
    channels: tuple[ChannelWorkbookRow, ...]


def _workbook_content(workbook_path: Path | None = None) -> bytes:
    try:
        if workbook_path is not None:
            return workbook_path.expanduser().resolve().read_bytes()
        return (
            files("a6api_auto")
            .joinpath("data/merchant_templates_split.xlsx")
            .read_bytes()
        )
    except OSError as exc:
        raise A6AutoError("workbook_template_catalog_unreadable") from exc


def _strict_workbook_rows(
    rows: tuple[XlsxRow, ...],
    *,
    headers: tuple[str, ...],
) -> tuple[XlsxRow, ...]:
    if rows[0].values != headers:
        raise A6AutoError("workbook_template_headers_invalid")
    values = rows[1:]
    if len(values) != WORKBOOK_TEMPLATE_COUNT:
        raise A6AutoError("workbook_template_count_invalid")
    if any(len(row.values) != len(headers) or not all(row.values) for row in values):
        raise A6AutoError("workbook_template_rows_invalid")
    return values


def load_workbook_template_catalog(
    workbook_path: Path | None = None,
) -> WorkbookTemplateCatalog:
    content = _workbook_content(workbook_path)
    supplier_rows = _strict_workbook_rows(
        read_xlsx_sheet(content, WORKBOOK_SUPPLIER_SHEET),
        headers=WORKBOOK_SUPPLIER_HEADERS,
    )
    channel_rows = _strict_workbook_rows(
        read_xlsx_sheet(content, WORKBOOK_CHANNEL_SHEET),
        headers=WORKBOOK_CHANNEL_HEADERS,
    )
    suppliers = tuple(
        SupplierWorkbookRow(row.source_row, row.values[0], row.values[1])
        for row in supplier_rows
    )
    channels = tuple(
        ChannelWorkbookRow(
            row.source_row,
            row.values[0],
            row.values[1],
            row.values[2],
        )
        for row in channel_rows
    )
    if any(
        not 1 <= len(item.supplier_name) <= 30
        or not item.supplier_description
        or any(marker in item.supplier_name for marker in _PUBLIC_GROUP_MARKERS)
        or any(marker in item.supplier_description for marker in _PUBLIC_GROUP_MARKERS)
        for item in suppliers
    ):
        raise A6AutoError("workbook_supplier_template_invalid")
    if any(
        not 1 <= len(item.channel_name) <= 30
        or is_forbidden_channel_name(item.channel_name)
        or not item.channel_description
        or not item.model_description
        or any(
            marker in field
            for marker in _PUBLIC_GROUP_MARKERS
            for field in (
                item.channel_name,
                item.channel_description,
                item.model_description,
            )
        )
        for item in channels
    ):
        raise A6AutoError("workbook_channel_template_invalid")
    unique_fields = (
        [item.supplier_name for item in suppliers],
        [item.supplier_description for item in suppliers],
        [item.channel_name for item in channels],
        [item.channel_description for item in channels],
    )
    if any(len(values) != len(set(values)) for values in unique_fields):
        raise A6AutoError("workbook_template_values_duplicate")
    return WorkbookTemplateCatalog(
        sha256=hashlib.sha256(content).hexdigest(),
        suppliers=suppliers,
        channels=channels,
    )


def _workbook_channel_nickname(channel_name: str) -> str:
    nickname = re.sub(r"\s+", " ", channel_name).strip()[:10]
    if not nickname:
        raise A6AutoError("workbook_channel_nickname_invalid")
    return nickname


def _workbook_template_id(supplier_row: int, channel_row: int) -> str:
    return f"workbook-s{supplier_row:04d}-c{channel_row:04d}"


def _workbook_template(
    supplier: SupplierWorkbookRow,
    channel: ChannelWorkbookRow,
) -> MerchantTemplate:
    return MerchantTemplate(
        template_id=_workbook_template_id(supplier.source_row, channel.source_row),
        supplier_name=supplier.supplier_name,
        supplier_description=supplier.supplier_description,
        channel_name=channel.channel_name,
        channel_nickname=_workbook_channel_nickname(channel.channel_name),
        channel_description=channel.channel_description,
        model_description=channel.model_description,
        channel_type=1,
    )


def choose_workbook_template(
    template_id: str,
    *,
    workbook_path: Path | None = None,
) -> MerchantTemplate:
    match = WORKBOOK_TEMPLATE_ID.fullmatch(str(template_id or "").strip())
    if match is None:
        raise A6AutoError("template_not_found")
    catalog = load_workbook_template_catalog(workbook_path)
    supplier_row = int(match.group("supplier"))
    channel_row = int(match.group("channel"))
    suppliers = {item.source_row: item for item in catalog.suppliers}
    channels = {item.source_row: item for item in catalog.channels}
    try:
        return _workbook_template(suppliers[supplier_row], channels[channel_row])
    except KeyError as exc:
        raise A6AutoError("template_not_found") from exc


def _identity_terms(value: str) -> frozenset[str]:
    lowered = value.casefold()
    ascii_terms = {
        item
        for item in re.findall(r"[a-z0-9]+", lowered)
        if len(item) >= 3 and item not in _GENERIC_IDENTITY_TERMS
    }
    chinese = "".join(re.findall(r"[\u3400-\u9fff]", lowered))
    chinese_terms = {
        chinese[index : index + 2]
        for index in range(max(0, len(chinese) - 1))
        if chinese[index : index + 2] not in _GENERIC_IDENTITY_TERMS
    }
    return frozenset(ascii_terms | chinese_terms)


def _identity_style(value: str) -> str:
    has_chinese = re.search(r"[\u3400-\u9fff]", value) is not None
    has_latin = re.search(r"[A-Za-z]", value) is not None
    if has_chinese and has_latin:
        return "mixed"
    return "cjk" if has_chinese else "latin"


def _description_terms(value: str, *, width: int = 6) -> frozenset[str]:
    compact = "".join(re.findall(r"[a-z0-9\u3400-\u9fff]", value.casefold()))
    return frozenset(
        compact[index : index + width]
        for index in range(max(0, len(compact) - width + 1))
    )


def _profiles_are_independent(
    pairs: tuple[tuple[SupplierWorkbookRow, ChannelWorkbookRow], ...],
) -> bool:
    seen_name_terms: set[str] = set()
    seen_description_terms: set[str] = set()
    for supplier, channel in pairs:
        name_terms = set(_identity_terms(supplier.supplier_name))
        name_terms.update(_identity_terms(channel.channel_name))
        description_terms = set(_description_terms(supplier.supplier_description))
        description_terms.update(_description_terms(channel.channel_description))
        if seen_name_terms.intersection(name_terms) or seen_description_terms.intersection(
            description_terms
        ):
            return False
        seen_name_terms.update(name_terms)
        seen_description_terms.update(description_terms)
    return True


def _diverse_sample(
    values: list[Any],
    count: int,
    *,
    name: Any,
    randomizer: Any,
) -> list[Any]:
    if count > 12:
        return list(randomizer.sample(values, count))
    buckets = {
        style: [item for item in values if _identity_style(name(item)) == style]
        for style in ("cjk", "latin", "mixed")
    }
    sequence = ("cjk", "latin", "mixed")
    desired = [sequence[index % len(sequence)] for index in range(count)]
    selected: list[Any] = []
    for style in desired:
        candidates = [item for item in buckets[style] if item not in selected]
        if not candidates:
            return list(randomizer.sample(values, count))
        selected.append(randomizer.choice(candidates))
    randomizer.shuffle(selected)
    return selected


def _model_source_families(value: str) -> frozenset[str]:
    lowered = value.casefold()
    without_bugteam = lowered.replace("bugteam", "")
    families = {
        family
        for family in ("pro", "plus", "k12", "team")
        if family in without_bugteam
    }
    if "bugteam" in lowered:
        families.add("bugteam")
    return frozenset(families)


def _source_diverse_channel_sample(
    values: list[ChannelWorkbookRow],
    count: int,
    *,
    randomizer: Any,
) -> list[ChannelWorkbookRow]:
    if count > len(_MODEL_SOURCE_FAMILIES):
        styles = ("cjk", "latin", "mixed")
        desired_styles = [styles[index % len(styles)] for index in range(count)]
        for _attempt in range(64):
            randomizer.shuffle(desired_styles)
            selected: list[ChannelWorkbookRow] = []
            model_descriptions: set[str] = set()
            for style in desired_styles:
                candidates = [
                    item
                    for item in values
                    if item not in selected
                    and item.model_description not in model_descriptions
                    and _identity_style(item.channel_name) == style
                ]
                if not candidates:
                    break
                chosen = randomizer.choice(candidates)
                selected.append(chosen)
                model_descriptions.add(chosen.model_description)
            if len(selected) == count:
                randomizer.shuffle(selected)
                return selected
        raise A6AutoError("workbook_template_source_diversity_unavailable")
    families = list(randomizer.sample(_MODEL_SOURCE_FAMILIES, count))
    styles = ("latin", "mixed", "cjk")
    selected: list[ChannelWorkbookRow] = []
    for index, family in enumerate(families):
        candidates = [
            item
            for item in values
            if item not in selected
            and _identity_style(item.channel_name) == styles[index % len(styles)]
            and _model_source_families(item.model_description) == {family}
        ]
        if not candidates:
            raise A6AutoError("workbook_template_source_diversity_unavailable")
        selected.append(randomizer.choice(candidates))
    randomizer.shuffle(selected)
    return selected


def _independent_profile_sample(
    suppliers: list[SupplierWorkbookRow],
    channels: list[ChannelWorkbookRow],
    count: int,
    *,
    randomizer: Any,
) -> tuple[tuple[SupplierWorkbookRow, ChannelWorkbookRow], ...]:
    style_pairs = (
        ("cjk", "latin"),
        ("latin", "mixed"),
        ("mixed", "cjk"),
        ("cjk", "mixed"),
        ("latin", "cjk"),
        ("mixed", "latin"),
    )
    supplier_profiles = tuple(
        (
            item,
            _identity_terms(item.supplier_name),
            _description_terms(item.supplier_description),
        )
        for item in suppliers
    )
    channel_profiles = tuple(
        (
            item,
            _identity_terms(item.channel_name),
            _description_terms(item.channel_description),
        )
        for item in channels
    )
    for _selection_attempt in range(128):
        desired_styles: list[tuple[str, str]] = []
        while len(desired_styles) < count:
            remaining = min(count - len(desired_styles), len(style_pairs))
            desired_styles.extend(randomizer.sample(style_pairs, remaining))
        randomizer.shuffle(desired_styles)
        requested_families = (
            list(randomizer.sample(_MODEL_SOURCE_FAMILIES, count))
            if count <= len(_MODEL_SOURCE_FAMILIES)
            else []
        )
        randomizer.shuffle(requested_families)
        supplier_order = list(supplier_profiles)
        channel_order = list(channel_profiles)
        randomizer.shuffle(supplier_order)
        randomizer.shuffle(channel_order)
        selected: list[tuple[SupplierWorkbookRow, ChannelWorkbookRow]] = []
        selected_supplier_rows: set[int] = set()
        selected_channel_rows: set[int] = set()
        seen_name_terms: set[str] = set()
        seen_description_terms: set[str] = set()
        seen_model_descriptions: set[str] = set()
        seen_source_families: set[frozenset[str]] = set()
        seen_nicknames: set[str] = set()

        for index, (supplier_style, channel_style) in enumerate(desired_styles):
            requested_family = requested_families[index] if requested_families else ""
            match: tuple[
                SupplierWorkbookRow,
                frozenset[str],
                frozenset[str],
                ChannelWorkbookRow,
                frozenset[str],
                frozenset[str],
            ] | None = None
            for supplier, supplier_names, supplier_descriptions in supplier_order:
                if (
                    supplier.source_row in selected_supplier_rows
                    or _identity_style(supplier.supplier_name) != supplier_style
                    or seen_name_terms.intersection(supplier_names)
                    or seen_description_terms.intersection(supplier_descriptions)
                ):
                    continue
                candidates = [
                    (channel, channel_names, channel_descriptions)
                    for channel, channel_names, channel_descriptions in channel_order
                    if channel.source_row not in selected_channel_rows
                    and channel.source_row != supplier.source_row
                    and _identity_style(channel.channel_name) == channel_style
                    and channel.model_description not in seen_model_descriptions
                    and _workbook_channel_nickname(channel.channel_name)
                    not in seen_nicknames
                    and not (seen_name_terms | set(supplier_names)).intersection(
                        channel_names
                    )
                    and not seen_description_terms.intersection(channel_descriptions)
                    and (
                        not requested_family
                        or _model_source_families(channel.model_description)
                        == {requested_family}
                    )
                ]
                if not candidates:
                    continue
                unseen_family_candidates = [
                    candidate
                    for candidate in candidates
                    if _model_source_families(candidate[0].model_description)
                    not in seen_source_families
                ]
                channel, channel_names, channel_descriptions = randomizer.choice(
                    unseen_family_candidates or candidates
                )
                match = (
                    supplier,
                    supplier_names,
                    supplier_descriptions,
                    channel,
                    channel_names,
                    channel_descriptions,
                )
                break
            if match is None:
                break
            (
                supplier,
                supplier_names,
                supplier_descriptions,
                channel,
                channel_names,
                channel_descriptions,
            ) = match
            selected.append((supplier, channel))
            selected_supplier_rows.add(supplier.source_row)
            selected_channel_rows.add(channel.source_row)
            seen_name_terms.update(supplier_names)
            seen_name_terms.update(channel_names)
            seen_description_terms.update(supplier_descriptions)
            seen_description_terms.update(channel_descriptions)
            seen_model_descriptions.add(channel.model_description)
            seen_source_families.add(_model_source_families(channel.model_description))
            seen_nicknames.add(_workbook_channel_nickname(channel.channel_name))

        pairs = tuple(selected)
        if len(pairs) == count and _profiles_are_independent(pairs):
            randomizer.shuffle(selected)
            return tuple(selected)
    raise A6AutoError("workbook_template_independent_pair_unavailable")


def sample_workbook_template_ids(
    count: int,
    *,
    used_template_ids: set[str] | None = None,
    workbook_path: Path | None = None,
    random_source: Any | None = None,
) -> tuple[str, ...]:
    if isinstance(count, bool) or not 1 <= count <= WORKBOOK_TEMPLATE_COUNT:
        raise A6AutoError("workbook_template_sample_count_invalid")
    catalog = load_workbook_template_catalog(workbook_path)
    used_supplier_rows: set[int] = set()
    used_channel_rows: set[int] = set()
    for template_id in used_template_ids or set():
        match = WORKBOOK_TEMPLATE_ID.fullmatch(str(template_id))
        if match is not None:
            used_supplier_rows.add(int(match.group("supplier")))
            used_channel_rows.add(int(match.group("channel")))
    suppliers = [
        item
        for item in catalog.suppliers
        if item.source_row not in used_supplier_rows
        and not any(character.isdigit() for character in item.supplier_name)
    ]
    channels = [
        item
        for item in catalog.channels
        if item.source_row not in used_channel_rows
        and not any(character.isdigit() for character in item.channel_name)
    ]
    if min(len(suppliers), len(channels)) < count:
        raise A6AutoError("workbook_template_pool_smaller_than_target")

    randomizer = random_source or secrets.SystemRandom()
    natural_channels = [
        item for item in channels if not _MECHANICAL_CHANNEL_NAME.search(item.channel_name)
    ]
    if len(natural_channels) < count:
        raise A6AutoError("workbook_template_natural_channel_pool_too_small")
    pairs = _independent_profile_sample(
        suppliers,
        natural_channels,
        count,
        randomizer=randomizer,
    )
    return tuple(
        _workbook_template_id(supplier.source_row, channel.source_row)
        for supplier, channel in pairs
    )


def validate_registration_template(
    template: MerchantTemplate,
    *,
    workbook_path: Path | None = None,
) -> MerchantTemplate:
    """Reject legacy correlated identities before any registration request is sent."""
    if WORKBOOK_TEMPLATE_ID.fullmatch(template.template_id):
        expected = choose_workbook_template(
            template.template_id,
            workbook_path=workbook_path,
        )
        if template != expected:
            raise A6AutoError("workbook_template_content_mismatch")
        if any(
            character.isdigit()
            for value in (template.supplier_name, template.channel_name)
            for character in value
        ):
            raise A6AutoError("batch_template_naming_rejected")
        return template
    public_text = "\n".join(
        (
            template.supplier_name,
            template.supplier_description,
            template.channel_name,
            template.channel_description,
        )
    )
    supplier_name = template.supplier_name.strip()
    normalized_name = supplier_name.casefold()
    rejected = (
        not supplier_name
        or not any(marker in normalized_name for marker in _RELAY_NAME_MARKERS)
        or _NUMBERED_CHINESE_NAME.fullmatch(supplier_name) is not None
        or _NUMBERED_CHANNEL.search(public_text) is not None
        or any(value in public_text for value in _FORBIDDEN_REGISTRATION_TEXT)
    )
    if rejected:
        raise A6AutoError("batch_template_naming_rejected")
    return template


def load_templates() -> tuple[MerchantTemplate, ...]:
    resource = files("a6api_auto").joinpath("data/merchant_templates.json")
    try:
        values = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise A6AutoError("template_catalog_unreadable") from exc
    if not isinstance(values, list) or len(values) != 200:
        raise A6AutoError("template_catalog_shape_invalid")
    try:
        templates = tuple(MerchantTemplate.from_dict(item) for item in values)
    except (TypeError, ValueError) as exc:
        raise A6AutoError("template_catalog_shape_invalid") from exc
    for template in templates:
        validate_registration_template(template)
    return templates


def choose_template(
    template_id: str = "",
    *,
    workbook_path: Path | None = None,
) -> MerchantTemplate:
    if WORKBOOK_TEMPLATE_ID.fullmatch(str(template_id or "").strip()):
        return choose_workbook_template(
            str(template_id).strip(),
            workbook_path=workbook_path,
        )
    templates = load_templates()
    if not template_id:
        return secrets.choice(templates)
    matches = [item for item in templates if item.template_id == template_id]
    if len(matches) != 1:
        raise A6AutoError("template_not_found")
    return matches[0]


def choose_account_template(template_id: str, *, a6_user_id: int) -> MerchantTemplate:
    """Resolve the recorded template, with a stable fallback for manually imported accounts."""
    if str(template_id or "").strip():
        return choose_template(str(template_id).strip())
    if isinstance(a6_user_id, bool) or not isinstance(a6_user_id, int) or a6_user_id < 1:
        raise A6AutoError("template_account_identity_invalid")
    templates = load_templates()
    return templates[(a6_user_id - 1) % len(templates)]
