from __future__ import annotations

from getpass import getpass

from sqlalchemy import select

from app.core.database import SessionLocal, init_db
from app.core.security import hash_password
from app.models.entities import User, UserRole


def prompt_non_empty(label: str) -> str:
    while True:
        value = input(f"{label}: ").strip()
        if value:
            return value
        print(f"{label} es obligatorio.")


def prompt_password() -> str:
    while True:
        password = getpass("Contraseña: ").strip()
        if len(password) < 8:
            print("La contraseña debe tener al menos 8 caracteres.")
            continue

        confirm_password = getpass("Confirmar contraseña: ").strip()
        if password != confirm_password:
            print("Las contraseñas no coinciden.")
            continue

        return password


def main() -> int:
    init_db()

    email = prompt_non_empty("Email").lower()
    full_name = prompt_non_empty("Nombre")
    password = prompt_password()

    db = SessionLocal()
    try:
        existing_user = db.scalar(select(User).where(User.email == email))
        if existing_user:
            if existing_user.role == UserRole.SUPER_ADMIN:
                print(f"Ya existe un Super Admin con el correo {email}. No se creó un duplicado.")
                return 0

            print(
                f"Ya existe un usuario con el correo {email} y rol {existing_user.role.value}. "
                "No se modificó nada.",
            )
            return 0

        existing_super_admin = db.scalar(select(User).where(User.role == UserRole.SUPER_ADMIN).limit(1))
        if existing_super_admin:
            print(
                "Ya existe un Super Admin en el sistema "
                f"({existing_super_admin.email}). Este script solo crea el primer Super Admin.",
            )
            return 0

        user = User(
            email=email,
            full_name=full_name,
            password_hash=hash_password(password),
            role=UserRole.SUPER_ADMIN,
            status="active",
            is_active=True,
        )
        db.add(user)
        db.commit()
        print(f"Super Admin creado correctamente: {email}")
        return 0
    except KeyboardInterrupt:
        db.rollback()
        print("\nOperación cancelada.")
        return 1
    except Exception as exc:
        db.rollback()
        print(f"Error al crear el Super Admin: {exc}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
