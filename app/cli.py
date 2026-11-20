"""Command-line helpers.

python -m app.cli create-user --email you@idealz.lk --name "Your Name" --role admin
python -m app.cli seed-demo          # sample data for training (not in production)
"""

from __future__ import annotations

import argparse
import getpass
import random
import sys
from datetime import timedelta

from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal, get_engine, utcnow
from app.models import ActivityKind, Channel, ContactOutcome, LostReason, Role, Timeframe, User
from app.security import WeakPassword, hash_password
from app.services import inquiries as svc
from app.services.bootstrap import seed_default_channels


def create_user(args: argparse.Namespace) -> int:
    password = args.password or getpass.getpass("Password: ")
    get_engine()
    with SessionLocal() as db:
        email = args.email.strip().lower()
        if db.scalar(select(User).where(func.lower(User.email) == email)):
            print(f"User {email} already exists.", file=sys.stderr)
            return 1
        try:
            user = User(
                full_name=args.name,
                email=email,
                role=Role(args.role),
                password_hash=hash_password(password),
            )
        except WeakPassword as exc:
            print(exc, file=sys.stderr)
            return 1
        db.add(user)
        db.commit()
        print(f"Created {user.role.value} {user.email}")
    return 0


DEMO_PRODUCTS = [
    "iPhone 16 Pro 256GB",
    "iPhone 16 128GB",
    "iPhone 15 128GB",
    "Samsung Galaxy S25 Ultra",
    "Samsung Galaxy A56",
    "Google Pixel 9",
    "AirPods Pro 2",
    "Apple Watch Series 10",
]
DEMO_NAMES = [
    "Nimal",
    "Kasun",
    "Ayesha",
    "Fathima",
    "Dilshan",
    "Tharindu",
    "Sachini",
    "Ruwan",
    None,
]


def seed_demo(args: argparse.Namespace) -> int:
    settings = get_settings()
    if settings.is_production and not args.force:
        print("Refusing to load demo data in production (use --force).", file=sys.stderr)
        return 1
    rng = random.Random(42)
    get_engine()
    with SessionLocal() as db:
        seed_default_channels(db)
        channels = list(db.scalars(select(Channel).where(Channel.is_active.is_(True))))
        sales = list(db.scalars(select(User).where(User.role == Role.SALES)))
        if not sales:
            for name in ("Sales One", "Sales Two"):
                user = User(
                    full_name=name,
                    email=name.lower().replace(" ", ".") + "@example.com",
                    role=Role.SALES,
                    password_hash=hash_password("demo-password"),
                )
                db.add(user)
                sales.append(user)
            db.flush()
        coordinator = db.scalar(select(User).where(User.role != Role.SALES)) or sales[0]
        now = utcnow()
        for n in range(args.count):
            created = now - timedelta(hours=rng.randint(0, 24 * 20))
            clean = svc.clean_inquiry_input(
                db,
                svc.InquiryInput(
                    phone=f"07{rng.choice('0125678')}{rng.randint(1000000, 9999999)}",
                    channel_id=rng.choice(channels).id,
                    product_text=rng.choice(DEMO_PRODUCTS),
                    timeframe=rng.choice(list(Timeframe)),
                    customer_name=rng.choice(DEMO_NAMES),
                    assigned_to_id=rng.choice(sales).id if rng.random() > 0.15 else None,
                ),
                settings,
            )
            inquiry = svc.create_inquiry(db, clean, coordinator, settings, created)
            worker = inquiry.assigned_to or coordinator
            for step in range(rng.randint(0, 3)):
                when = created + timedelta(minutes=rng.randint(5, 600) + step * 1440)
                if when > now:
                    break
                outcome = rng.choice(
                    [
                        ContactOutcome.REACHED,
                        ContactOutcome.NO_ANSWER,
                        ContactOutcome.PRICE_SENT,
                        ContactOutcome.WON,
                        ContactOutcome.LOST,
                    ]
                )
                svc.log_contact(
                    db,
                    inquiry,
                    worker,
                    rng.choice([ActivityKind.CALL, ActivityKind.WHATSAPP]),
                    outcome,
                    None,
                    settings,
                    when,
                    lost_reason=rng.choice(list(LostReason))
                    if outcome == ContactOutcome.LOST
                    else None,
                )
                if not inquiry.is_open:
                    break
            if n % 50 == 0:
                db.flush()
        db.commit()
        print(f"Loaded {args.count} demo inquiries.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_user = sub.add_parser("create-user", help="Create a staff account")
    p_user.add_argument("--email", required=True)
    p_user.add_argument("--name", required=True)
    p_user.add_argument("--role", choices=[r.value for r in Role], default=Role.SALES.value)
    p_user.add_argument("--password", help="Omit to be prompted securely")
    p_user.set_defaults(func=create_user)

    p_demo = sub.add_parser("seed-demo", help="Load sample inquiries for training")
    p_demo.add_argument("--count", type=int, default=120)
    p_demo.add_argument("--force", action="store_true")
    p_demo.set_defaults(func=seed_demo)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
