"""P44d: disposable signup emails are refused."""

from __future__ import annotations

import pytest

from app.errors import ValidationFailedError
from app.repositories import users as users_repo
from app.services import email_policy


@pytest.mark.parametrize(
    ("email", "blocked"),
    [
        ("someone@mailinator.com", True),
        ("SomeOne@Mailinator.COM", True),
        ("x@sub.mailinator.com", True),
        ("a@yopmail.com", True),
        ("a@guerrillamail.com", True),
        ("owner@gmail.com", False),
        ("owner@outlook.com", False),
        ("owner@sabinepropertygroup.net", False),
        ("not-an-email", False),
        ("", False),
    ],
)
def test_is_disposable(email, blocked):
    assert email_policy.is_disposable(email) is blocked


def test_the_bundled_list_is_loaded():
    assert len(email_policy._domains()) > 1000


async def test_signup_with_a_disposable_email_is_refused(session):
    with pytest.raises(ValidationFailedError):
        await users_repo.create_user(session, email="burner@mailinator.com", password="x" * 16)


async def test_register_endpoint_refuses_disposable_email(client):
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "burner@yopmail.com",
            "password": "correct horse battery staple 9",
            "full_name": "Burner",
            "org_name": "Burner LLC",
        },
    )
    assert resp.status_code == 422, resp.text
    assert "temporary" in resp.text
