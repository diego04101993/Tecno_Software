import { useEffect, useState } from "react";

import type { SessionUser, UserRole, UserStatus } from "../../types/domain";

type TeamUserFormDialogProps = {
  open: boolean;
  mode: "create" | "edit";
  initialUser?: SessionUser | null;
  errorMessage?: string | null;
  isSaving?: boolean;
  onCancel: () => void;
  onSubmit: (payload: {
    full_name: string;
    email: string;
    password: string;
    role: UserRole;
    status: UserStatus;
  }) => void;
};

type FormState = {
  full_name: string;
  email: string;
  password: string;
  role: UserRole;
  status: UserStatus;
};

const defaultForm: FormState = {
  full_name: "",
  email: "",
  password: "",
  role: "staff_admin",
  status: "active",
};

const roleOptions: Array<{ value: UserRole; label: string }> = [
  { value: "super_admin", label: "Super admin" },
  { value: "staff_admin", label: "Staff admin" },
  { value: "staff_operator", label: "Staff operator" },
];

export function TeamUserFormDialog({
  open,
  mode,
  initialUser = null,
  errorMessage = null,
  isSaving = false,
  onCancel,
  onSubmit,
}: TeamUserFormDialogProps) {
  const [form, setForm] = useState<FormState>(defaultForm);
  const [passwordError, setPasswordError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    setPasswordError(null);
    if (initialUser) {
      setForm({
        full_name: initialUser.full_name,
        email: initialUser.email,
        password: "",
        role: initialUser.role,
        status: initialUser.status,
      });
      return;
    }
    setForm(defaultForm);
  }, [initialUser, open]);

  function handleSubmit() {
    const passwordLength = Array.from(form.password).length;
    if ((mode === "create" || form.password !== "") &&
        (passwordLength < 15 || passwordLength > 128 || !form.password.trim())) {
      setPasswordError("La contrasena debe tener entre 15 y 128 caracteres y no ser solo espacios.");
      return;
    }
    setPasswordError(null);
    onSubmit(form);
  }

  if (!open) {
    return null;
  }

  return (
    <div className="fixed inset-0 z-[95] flex items-center justify-center bg-slate-950/55 px-4 backdrop-blur-sm">
      <div className="w-full max-w-2xl rounded-[28px] border border-slate-300 bg-white p-6 shadow-2xl">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs uppercase tracking-[0.28em] text-accent">Equipo interno</p>
            <h3 className="mt-2 text-2xl font-semibold text-ink">{mode === "create" ? "Crear usuario interno" : "Editar usuario interno"}</h3>
          </div>
          <button
            className="rounded-full border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700"
            type="button"
            onClick={onCancel}
          >
            Cerrar
          </button>
        </div>

        <div className="mt-6 grid gap-4">
          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
            <input value={form.full_name} onChange={(event) => setForm({ ...form, full_name: event.target.value })} />
          </div>
          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Email</label>
            <input type="email" value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} />
          </div>
          <div className="grid gap-4 md:grid-cols-3">
            <div className="md:col-span-2">
              <label className="mb-2 block text-sm font-semibold text-slate-700">{mode === "create" ? "Contrasena temporal" : "Nueva clave (opcional)"}</label>
              <input type="password" autoComplete="new-password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} />
              <p className="mt-1 text-xs text-slate-600">15–128 caracteres. Se permiten frases y espacios.</p>
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Estado</label>
              <select value={form.status} onChange={(event) => setForm({ ...form, status: event.target.value as UserStatus })}>
                <option value="active">Activo</option>
                <option value="suspended">Suspendido</option>
              </select>
            </div>
          </div>
          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Rol</label>
            <select value={form.role} onChange={(event) => setForm({ ...form, role: event.target.value as UserRole })}>
              {roleOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          {passwordError ? <div role="alert" className="text-sm text-rose-700">{passwordError}</div> : null}
          {errorMessage ? <div className="rounded-2xl bg-rose-50 px-4 py-3 text-sm text-rose-700">{errorMessage}</div> : null}

          <div className="flex justify-end gap-3">
            <button
              className="rounded-full border border-slate-300 px-4 py-2.5 text-sm font-semibold text-slate-700"
              type="button"
              onClick={onCancel}
            >
              Cancelar
            </button>
            <button
              className="rounded-full bg-ink px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-60"
              disabled={isSaving}
              type="button"
              onClick={handleSubmit}
            >
              {isSaving ? "Guardando..." : mode === "create" ? "Crear usuario" : "Guardar cambios"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
