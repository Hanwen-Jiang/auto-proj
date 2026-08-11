from __future__ import annotations

from a6api_auto.enodot import EnodotGroupKeyProvider


class Response:
    status_code = 200

    def __init__(self, data) -> None:
        self.data = data

    def json(self):
        return {"code": 0, "message": "success", "data": self.data}


class Session:
    def __init__(self, responses) -> None:
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(next(self.responses))


def groups():
    return [
        {"id": 3, "name": "Other", "status": "active"},
        {"id": 17, "name": "TPA6API", "status": "active"},
    ]


def keys():
    return {
        "items": [
            {"id": 41, "key": "sk-inactive", "status": "inactive"},
            {"id": 42, "key": "sk-active-a", "status": "active"},
            {"id": 43, "key": "sk-active-b", "status": "active"},
        ],
        "total": 3,
        "page": 1,
        "page_size": 200,
        "pages": 1,
    }


def test_provider_selects_active_group_key_without_exposing_admin_key() -> None:
    session = Session((groups(), keys()))
    provider = EnodotGroupKeyProvider(
        "https://ai.enodot.shop",
        "admin-fixture-secret",
        "TPA6API",
        ("https://domain-a.test", "https://domain-b.test"),
        session=session,
        choice=lambda values: values[-1],
    )

    selected = provider.select()

    assert selected.base_url == "https://domain-b.test"
    assert selected.api_key == "sk-active-b"
    assert selected.key_ref == "43"
    assert session.calls[0][0].endswith("/api/v1/admin/groups/all")
    assert session.calls[1][0].endswith("/api/v1/admin/groups/17/api-keys")
    assert all(call[1]["headers"]["x-api-key"] == "admin-fixture-secret" for call in session.calls)


def test_provider_resumes_exact_domain_and_key_id() -> None:
    session = Session((groups(), keys()))
    provider = EnodotGroupKeyProvider(
        "https://ai.enodot.shop",
        "admin-fixture-secret",
        "TPA6API",
        ("https://domain-a.test", "https://domain-b.test"),
        session=session,
        choice=lambda values: values[-1],
    )

    selected = provider.select({"base_url": "https://domain-a.test", "key_ref": "42"})

    assert selected.base_url == "https://domain-a.test"
    assert selected.api_key == "sk-active-a"
    assert selected.key_ref == "42"


def test_provider_accepts_runtime_alias_when_exact_group_name_absent() -> None:
    session = Session((
        [
            {"id": 26, "name": "ToA6API", "status": "active"},
            {"id": 3, "name": "Other", "status": "active"},
        ],
        keys(),
    ))
    provider = EnodotGroupKeyProvider(
        "https://ai.enodot.shop",
        "admin-fixture-secret",
        "TPA6API",
        ("https://domain-a.test", "https://domain-b.test"),
        session=session,
        choice=lambda values: values[-1],
    )

    selected = provider.select()

    assert selected.base_url == "https://domain-b.test"
    assert selected.api_key == "sk-active-b"
    assert selected.key_ref == "43"
    assert session.calls[1][0].endswith("/api/v1/admin/groups/26/api-keys")
