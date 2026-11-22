from sqlalchemy import func, select

from app.models import Activity, Customer, Inquiry, InquiryStatus, Product, Role, User
from tests.conftest import PASSWORD, csrf_from, login, post_form


def _new_inquiry(client, channel_id, phone="0771234567", **extra):
    data = {
        "phone": phone,
        "customer_name": "Nimal",
        "channel_id": channel_id,
        "product_text": "iPhone 16 Pro 256GB",
        "timeframe": "this_week",
        **extra,
    }
    return post_form(client, "/inquiries/new", data, token_page="/inquiries/new")


# ---------------------------------------------------------------- auth & security
def test_pages_require_login(client):
    response = client.get("/inquiries?view=all", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/inquiries%3Fview%3Dall"


def test_wrong_password(client, make_user):
    user = make_user()
    token = csrf_from(client)
    response = client.post(
        "/login", data={"email": user.email, "password": "nope-nope", "csrf_token": token}
    )
    assert response.status_code == 400
    assert "Wrong email or password" in response.text


def test_login_is_throttled(client, make_user):
    user = make_user()
    token = csrf_from(client)
    for _ in range(8):
        client.post(
            "/login", data={"email": user.email, "password": "bad-pass", "csrf_token": token}
        )
    response = client.post(
        "/login", data={"email": user.email, "password": PASSWORD, "csrf_token": token}
    )
    assert response.status_code == 429


def test_post_without_csrf_is_rejected(client, make_user, channels):
    login(client, make_user(Role.COORDINATOR))
    response = client.post("/inquiries/new", data={"phone": "0771234567"})
    assert response.status_code == 403


def test_open_redirect_blocked(client, make_user):
    user = make_user()
    token = csrf_from(client)
    response = client.post(
        "/login",
        data={"email": user.email, "password": PASSWORD, "csrf_token": token, "next": "//evil.com"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/"


def test_security_headers(client):
    response = client.get("/login")
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_logout(client, make_user):
    login(client, make_user())
    post_form(client, "/logout", {})
    assert client.get("/", follow_redirects=False).status_code == 303


def test_change_password(client, make_user, db):
    user = make_user()
    login(client, user)
    response = post_form(
        client,
        "/account/password",
        {
            "current_password": PASSWORD,
            "new_password": "new-password-9",
            "confirm_password": "new-password-9",
        },
        token_page="/account/password",
    )
    assert response.status_code == 303
    db.expire_all()
    from app.security import verify_password

    assert verify_password("new-password-9", db.get(User, user.id).password_hash)


# ---------------------------------------------------------------- capture
def test_coordinator_logs_inquiry(client, make_user, channels, db):
    login(client, make_user(Role.COORDINATOR))
    response = _new_inquiry(client, channels[0].id)
    assert response.status_code == 303
    inquiry = db.scalar(select(Inquiry))
    assert response.headers["location"] == f"/inquiries/{inquiry.id}"
    assert inquiry.customer.phone_e164 == "+94771234567"
    assert inquiry.customer.name == "Nimal"
    page = client.get(response.headers["location"])
    assert "iPhone 16 Pro 256GB" in page.text
    assert "saved. Next follow-up" in page.text


def test_save_and_log_another_keeps_source(client, make_user, channels):
    login(client, make_user(Role.COORDINATOR))
    response = _new_inquiry(client, channels[2].id, action="save_new")
    assert response.headers["location"] == f"/inquiries/new?channel_id={channels[2].id}"
    form = client.get(response.headers["location"])
    assert (
        f'id="ch-{channels[2].id}" name="channel_id" value="{channels[2].id}" checked' in form.text
    )


def test_validation_errors_shown(client, make_user, channels):
    login(client, make_user(Role.COORDINATOR))
    response = _new_inquiry(client, channels[0].id, phone="12", product_text="")
    assert response.status_code == 422
    assert "is not a valid phone number" in response.text
    assert "What product is the customer interested in?" in response.text


def test_duplicate_warning_then_confirm(client, make_user, channels, db):
    login(client, make_user(Role.COORDINATOR))
    _new_inquiry(client, channels[0].id)
    response = _new_inquiry(client, channels[6].id, phone="+94 77 123 4567")
    assert response.status_code == 409
    assert "already has 1 open inquiry" in response.text
    response = _new_inquiry(
        client,
        channels[6].id,
        phone="+94 77 123 4567",
        confirm_duplicate="1",
        product_text="AirPods Pro 2",
    )
    assert response.status_code == 303
    assert db.scalar(select(func.count()).select_from(Inquiry)) == 2
    assert db.scalar(select(func.count()).select_from(Customer)) == 1


def test_product_catalogue_normalises_name(client, make_user, channels, db):
    db.add(Product(name="iPhone 16 Pro 256GB"))
    db.commit()
    login(client, make_user(Role.COORDINATOR))
    _new_inquiry(client, channels[0].id, product_text="iphone 16 pro 256gb")
    inquiry = db.scalar(select(Inquiry))
    assert inquiry.product_text == "iPhone 16 Pro 256GB"
    assert inquiry.product_id is not None


def test_customer_lookup(client, make_user, channels):
    login(client, make_user(Role.COORDINATOR))
    assert client.get("/customers/lookup?phone=077").json() == {"valid": False}
    new = client.get("/customers/lookup?phone=0779999999").json()
    assert new["valid"] and new["customer"] is None
    _new_inquiry(client, channels[0].id)
    known = client.get("/customers/lookup?phone=94771234567").json()
    assert known["customer"]["name"] == "Nimal"
    assert len(known["customer"]["open_inquiries"]) == 1


# ---------------------------------------------------------------- follow-up workflow
def test_sales_logs_followup_and_claims_lead(client, make_user, channels, db):
    coord = make_user(Role.COORDINATOR)
    rep = make_user(Role.SALES)
    login(client, coord)
    _new_inquiry(client, channels[0].id)
    inquiry_id = db.scalar(select(Inquiry.id))
    post_form(client, "/logout", {})

    login(client, rep)
    response = post_form(
        client,
        f"/inquiries/{inquiry_id}/log",
        {
            "kind": "whatsapp",
            "outcome": "price_sent",
            "note": "Sent Rs 389,000",
            "return_to": "/followups",
        },
        token_page=f"/inquiries/{inquiry_id}",
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/followups"
    db.expire_all()
    inquiry = db.get(Inquiry, inquiry_id)
    assert inquiry.status == InquiryStatus.PRICE_SENT
    assert inquiry.assigned_to_id == rep.id  # claimed by working it
    assert inquiry.followup_count == 1


def test_log_validation_error_rerenders(client, make_user, channels, db):
    login(client, make_user(Role.COORDINATOR))
    _new_inquiry(client, channels[0].id)
    inquiry_id = db.scalar(select(Inquiry.id))
    response = post_form(
        client,
        f"/inquiries/{inquiry_id}/log",
        {"kind": "call", "outcome": "lost"},
        token_page=f"/inquiries/{inquiry_id}",
    )
    assert response.status_code == 422
    assert "Why is the customer not buying?" in response.text


def test_sales_cannot_touch_colleagues_lead(client, make_user, channels, db):
    coord = make_user(Role.COORDINATOR)
    rep_a = make_user(Role.SALES, name="Rep A")
    rep_b = make_user(Role.SALES, name="Rep B")
    login(client, coord)
    _new_inquiry(client, channels[0].id, assigned_to_id=str(rep_a.id))
    inquiry_id = db.scalar(select(Inquiry.id))
    post_form(client, "/logout", {})
    login(client, rep_b)
    response = post_form(
        client,
        f"/inquiries/{inquiry_id}/log",
        {"kind": "call", "outcome": "reached"},
        token_page=f"/inquiries/{inquiry_id}",
    )
    assert response.status_code == 403
    assert client.get(f"/inquiries/{inquiry_id}/edit").status_code == 403


def test_edit_and_close_as_lost(client, make_user, channels, db):
    login(client, make_user(Role.COORDINATOR))
    _new_inquiry(client, channels[0].id)
    inquiry_id = db.scalar(select(Inquiry.id))
    base = {
        "phone": "0771234567",
        "customer_name": "Nimal",
        "channel_id": channels[0].id,
        "product_text": "iPhone 16 Pro 256GB",
        "timeframe": "this_month",
        "status": "lost",
    }
    page = f"/inquiries/{inquiry_id}/edit"
    assert post_form(client, page, base, token_page=page).status_code == 422
    response = post_form(client, page, {**base, "lost_reason": "price"}, token_page=page)
    assert response.status_code == 303
    db.expire_all()
    inquiry = db.get(Inquiry, inquiry_id)
    assert inquiry.status == InquiryStatus.LOST and inquiry.next_followup_at is None
    assert post_form(client, f"/inquiries/{inquiry_id}/reopen", {}).status_code == 303
    db.expire_all()
    assert db.get(Inquiry, inquiry_id).status == InquiryStatus.CONTACTED


def test_followups_and_dashboard_render(client, make_user, channels):
    rep = make_user(Role.SALES)
    login(client, make_user(Role.COORDINATOR))
    _new_inquiry(client, channels[0].id, assigned_to_id=str(rep.id), timeframe="today")
    assert client.get("/").status_code == 200
    page = client.get("/followups")
    assert page.status_code == 200 and "Nimal" in page.text
    assert "https://wa.me/94771234567?text=" in page.text
    assert client.get("/followups?owner=unassigned").status_code == 200


# ---------------------------------------------------------------- lists, reports, export
def test_list_filters_and_search(client, make_user, channels):
    login(client, make_user(Role.COORDINATOR))
    _new_inquiry(client, channels[0].id)
    _new_inquiry(
        client,
        channels[1].id,
        phone="0712345678",
        product_text="Galaxy S25",
        customer_name="Ayesha",
    )
    assert "Ayesha" in client.get("/inquiries?q=galaxy").text
    assert "Ayesha" not in client.get("/inquiries?q=0771234567").text
    assert "Nimal" in client.get("/inquiries?q=77123").text
    assert "Ayesha" in client.get(f"/inquiries?channel_id={channels[1].id}").text


def test_reports_permissions_and_render(client, make_user, channels):
    rep = make_user(Role.SALES)
    login(client, rep)
    assert client.get("/reports").status_code == 403
    post_form(client, "/logout", {})
    login(client, make_user(Role.COORDINATOR))
    _new_inquiry(client, channels[0].id)
    page = client.get("/reports")
    assert page.status_code == 200
    assert channels[0].name in page.text


def test_csv_export_manager_only(client, make_user, channels):
    login(client, make_user(Role.COORDINATOR))
    _new_inquiry(client, channels[0].id, notes="=HYPERLINK(evil)")
    assert client.get("/inquiries/export.csv").status_code == 403
    post_form(client, "/logout", {})
    login(client, make_user(Role.MANAGER))
    response = client.get("/inquiries/export.csv?view=all&assigned=")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    body = response.text
    assert "inquiry_id,created_at" in body
    assert "077 123 4567" in body
    assert "'=HYPERLINK(evil)" in body  # formula injection neutralised


# ---------------------------------------------------------------- admin
def test_admin_creates_user_and_sales_cannot(client, make_user, db):
    login(client, make_user(Role.SALES))
    assert client.get("/admin/users").status_code == 403
    post_form(client, "/logout", {})
    login(client, make_user(Role.ADMIN))
    response = post_form(
        client,
        "/admin/users/new",
        {
            "full_name": "Kasun Perera",
            "email": "Kasun@Idealz.lk",
            "role": "sales",
            "password": "temp-pass-1",
        },
        token_page="/admin/users/new",
    )
    assert response.status_code == 303
    assert db.scalar(select(User).where(User.email == "kasun@idealz.lk")).role == Role.SALES


def test_admin_cannot_lock_themselves_out(client, make_user):
    admin = make_user(Role.ADMIN)
    login(client, admin)
    response = post_form(
        client,
        f"/admin/users/{admin.id}",
        {"full_name": admin.full_name, "email": admin.email, "role": "sales", "is_active": "1"},
        token_page=f"/admin/users/{admin.id}",
    )
    assert response.status_code == 422


def test_manager_edits_channels_and_products(client, make_user, channels, db):
    login(client, make_user(Role.MANAGER))
    page = client.get("/admin/channels")
    assert page.status_code == 200
    response = post_form(
        client,
        "/admin/channels",
        {
            "channel_id": channels[0].id,
            "name": "Hotline 1 - Colombo 03",
            "kind": "whatsapp",
            "phone_number": "0770000001",
            "branch": "Colombo 03",
            "sort_order": 1,
            "is_active": "1",
        },
        token_page="/admin/channels",
    )
    assert response.status_code == 303
    db.expire_all()
    assert db.get(type(channels[0]), channels[0].id).name == "Hotline 1 - Colombo 03"
    post_form(
        client,
        "/admin/products",
        {"names": "iPhone 16\niphone 16\nPixel 9\n", "brand": ""},
        token_page="/admin/products",
    )
    assert db.scalar(select(func.count()).select_from(Product)) == 2


def test_customer_page_and_consent(client, make_user, channels, db):
    login(client, make_user(Role.COORDINATOR))
    _new_inquiry(client, channels[0].id)
    customer = db.scalar(select(Customer))
    assert client.get(f"/customers/{customer.id}").status_code == 200
    post_form(
        client,
        f"/customers/{customer.id}",
        {"name": "Nimal Perera", "notes": "VIP", "marketing_consent": "1"},
        token_page=f"/customers/{customer.id}",
    )
    db.expire_all()
    customer = db.get(Customer, customer.id)
    assert customer.marketing_consent and customer.consent_at is not None


def test_activity_audit_trail_records_actor(client, make_user, channels, db):
    coord = make_user(Role.COORDINATOR)
    login(client, coord)
    _new_inquiry(client, channels[0].id)
    activity = db.scalar(select(Activity))
    assert activity.user_id == coord.id


def test_404_page(client, make_user):
    login(client, make_user())
    response = client.get("/inquiries/999999")
    assert response.status_code == 404
    assert "Inquiry not found" in response.text


def test_log_inquiry_with_specific_date(client, make_user, channels, db, settings):
    from datetime import timedelta

    from app.db import utcnow

    login(client, make_user(Role.COORDINATOR))
    form = client.get("/inquiries/new").text
    assert "Pick a date" in form and 'name="planned_date"' in form
    missing = _new_inquiry(client, channels[0].id, timeframe="on_date")
    assert missing.status_code == 422
    assert "Pick the date the customer plans to buy" in missing.text

    planned = utcnow().astimezone(settings.tz).date() + timedelta(days=12)
    response = _new_inquiry(
        client, channels[0].id, timeframe="on_date", planned_date=planned.isoformat()
    )
    assert response.status_code == 303
    inquiry = db.scalar(select(Inquiry))
    assert inquiry.planned_date == planned
    page = client.get(response.headers["location"]).text
    assert inquiry.timeframe_label in page
    edit = client.get(f"/inquiries/{inquiry.id}/edit").text
    assert f'value="{planned.isoformat()}"' in edit
