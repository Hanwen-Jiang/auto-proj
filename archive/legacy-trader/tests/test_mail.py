import pytest

from a6api_auto.errors import A6AutoError
from a6api_auto.mail import MAX_MAILS, QQMailbox, extract_a6_code


def mail(mail_id: int, sender: str, subject: str, content: str) -> dict:
    return {
        "mail_id": mail_id,
        "sender": sender,
        "subject": subject,
        "content": content,
        "time": "2026-07-23 22:33:40",
    }


def test_extract_code_requires_exact_sender_and_subject() -> None:
    assert (
        extract_a6_code(
            mail(
                3,
                "A6API <noreply@notify.a6api.com>",
                "A6API 邮箱验证码",
                "您的验证码为：654321，请在十分钟内使用。",
            )
        )
        == "654321"
    )
    assert (
        extract_a6_code(
            mail(2, "Other <noreply@notify.a6api.com>", "A6API 邮箱验证码", "验证码 111111")
        )
        == ""
    )
    assert (
        extract_a6_code(mail(1, "A6API <noreply@notify.a6api.com>", "Other", "验证码 222222")) == ""
    )


def test_extract_code_accepts_alphanumeric_a6_codes() -> None:
    assert (
        extract_a6_code(
            mail(
                4,
                "A6API <noreply@notify.a6api.com>",
                "A6API 邮箱验证码",
                "您好，您的验证码为: 3d7925 ，10分钟内有效。",
            )
        )
        == "3d7925"
    )


def test_mailbox_snapshot_excludes_stale_code(monkeypatch) -> None:
    stale = mail(
        1,
        "A6API <noreply@notify.a6api.com>",
        "A6API 邮箱验证码",
        "验证码为 111111",
    )
    fresh = mail(
        2,
        "A6API <noreply@notify.a6api.com>",
        "A6API 邮箱验证码",
        "验证码为 654321",
    )
    responses = iter(
        (
            {"success": True, "mails": [stale]},
            {"success": True, "mails": [fresh, stale]},
        )
    )
    mailbox = QQMailbox(requester=lambda _url, _body: next(responses))

    seen = mailbox.snapshot("fixture@qq.com")
    code = mailbox.poll_code(
        "fixture@qq.com",
        seen,
        timeout_seconds=5,
        interval_seconds=3,
    )

    assert code == "654321"
    assert seen == {"1"}


def test_mailbox_retries_transient_api_rejection(monkeypatch) -> None:
    fresh = mail(
        2,
        "A6API <noreply@notify.a6api.com>",
        "A6API 邮箱验证码",
        "验证码为 654321",
    )
    responses = iter(({"success": False}, {"success": True, "mails": [fresh]}))
    monkeypatch.setattr("a6api_auto.mail.time.sleep", lambda _seconds: None)
    mailbox = QQMailbox(requester=lambda _url, _body: next(responses))

    assert (
        mailbox.poll_code(
            "fixture@qq.com",
            set(),
            timeout_seconds=5,
            interval_seconds=3,
        )
        == "654321"
    )


def test_mailbox_reports_account_login_failure_separately() -> None:
    mailbox = QQMailbox(
        requester=lambda _url, _body: {
            "success": True,
            "mails": {"error": "Login fail"},
        }
    )

    with pytest.raises(A6AutoError, match="qq_mailbox_unavailable"):
        mailbox.snapshot("fixture@qq.com")


def test_mailbox_keeps_only_latest_bounded_records() -> None:
    records = [
        mail(
            mail_id,
            "A6API <noreply@notify.a6api.com>",
            "A6API 邮箱验证码",
            f"验证码为 {mail_id:06d}",
        )
        for mail_id in range(MAX_MAILS + 20)
    ]
    mailbox = QQMailbox(requester=lambda _url, _body: {"success": True, "mails": records})

    seen = mailbox.snapshot("fixture@qq.com")

    assert len(seen) == MAX_MAILS
    assert str(MAX_MAILS + 19) in seen
    assert "0" not in seen
