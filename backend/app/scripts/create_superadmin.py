from __future__ import annotations

from getpass import getpass, GetPassWarning
import sys
import warnings

from app.core.credential_policy import validate_new_password

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
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise RuntimeError("Se requiere una terminal segura para introducir la contrasena.")
    while True:
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            try:
                password = getpass("Contrasena: ")
            except GetPassWarning:
                raise RuntimeError("No hay entrada segura para la contrasena.") from None
            try:
                validate_new_password(password)
            except ValueError as exc:
                print(str(exc))  # Fixed policy message, never the input.
                continue
            try:
                confirm_password = getpass("Confirmar contrasena: ")
            except GetPassWarning:
                raise RuntimeError("No hay entrada segura para la contrasena.") from None
        if password != confirm_password:
            print("Las contrasenas no coinciden.")
            continue
        return password


def main() -> int:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        print("Se requiere una terminal segura para crear el superadmin.")
        return 1
    try:
        email = prompt_non_empty("Email").lower()
        full_name = prompt_non_empty("Nombre")
        password = prompt_password()
    except (RuntimeError, ValueError, EOFError, KeyboardInterrupt):
        print("No se pudo completar la entrada segura.")
        return 1
    try:
        init_db()
        db = SessionLocal()
    except Exception:
        print("No se pudo inicializar el acceso a la base de datos.")
        return 1
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
    except Exception:
        db.rollback()
        print("No se pudo crear el superadmin; revisa la configuracion y disponibilidad de la base.")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
