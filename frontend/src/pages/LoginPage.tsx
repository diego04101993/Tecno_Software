import { MonitorPlay, PanelsTopLeft, TvMinimalPlay } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import { Navigate } from "react-router-dom";

import { consumeStoredAuthError, useAuth } from "../lib/auth";

export function LoginPage() {
  const { login, token } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const storedError = consumeStoredAuthError();
    if (storedError) {
      setError(storedError);
    }
  }, []);

  if (token) {
    return <Navigate to="/app" replace />;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);

    try {
      await login(email, password);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo iniciar sesion");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="grid min-h-screen bg-mist lg:grid-cols-[1.15fr_0.85fr]">
      <section className="relative overflow-hidden bg-ink px-8 py-10 text-white sm:px-12 lg:px-16">
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_top_left,_rgba(20,184,166,0.26),_transparent_26%),radial-gradient(circle_at_bottom_right,_rgba(249,115,22,0.28),_transparent_24%)]" />
        <div className="relative flex h-full flex-col justify-between">
          <div>
            <p className="text-xs uppercase tracking-[0.36em] text-teal-200">Tecno Control Cloud</p>
            <h1 className="mt-6 max-w-2xl font-display text-5xl leading-tight">
              Vende por pantalla, sincroniza videowalls y opera kioskos desde una sola plataforma SaaS.
            </h1>
            <p className="mt-6 max-w-xl text-base text-slate-300">
              Multi-tenant, canales escalables, campanas visuales, timeline operativo y una consola pensada para Mexico y
              LATAM.
            </p>
          </div>

          <div className="grid gap-4 md:grid-cols-3">
            {[
              {
                title: "Canales",
                text: "Pantallas individuales, HDMI extendido y grupos por sucursal.",
                icon: MonitorPlay,
              },
              {
                title: "Videowall",
                text: "Matrices 2x2, 3x1 o personalizadas con sync por timestamp.",
                icon: PanelsTopLeft,
              },
              {
                title: "Kiosko touch",
                text: "Hotspots, navegacion, modo attract y preparacion para POS.",
                icon: TvMinimalPlay,
              },
            ].map((feature) => {
              const Icon = feature.icon;
              return (
                <article key={feature.title} className="rounded-[28px] border border-white/10 bg-white/10 p-5 backdrop-blur">
                  <Icon className="h-6 w-6 text-teal-200" />
                  <p className="mt-4 font-display text-2xl">{feature.title}</p>
                  <p className="mt-2 text-sm text-slate-300">{feature.text}</p>
                </article>
              );
            })}
          </div>
        </div>
      </section>

      <section className="flex items-center justify-center px-6 py-10 sm:px-10">
        <div className="w-full max-w-lg rounded-[40px] border border-white/60 bg-white/80 p-8 shadow-card backdrop-blur">
          <p className="text-xs uppercase tracking-[0.34em] text-accent">Acceso administrativo</p>
          <h2 className="mt-4 font-display text-4xl text-ink">Entra a tu operacion digital.</h2>

          <form className="mt-8 space-y-4" onSubmit={handleSubmit} autoComplete="off">
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Correo</label>
              <input
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                type="email"
                autoComplete="email"
                required
              />
            </div>
            <div>
              <label className="mb-2 block text-sm font-semibold text-slate-700">Contrasena</label>
              <input
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                type="password"
                autoComplete="new-password"
                required
              />
            </div>
            {error ? <p className="rounded-2xl bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</p> : null}
            <button
              type="submit"
              disabled={submitting}
              className="w-full rounded-[20px] bg-ink px-5 py-4 font-semibold text-white transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {submitting ? "Entrando..." : "Entrar a la consola"}
            </button>
          </form>

          <div className="mt-6 border-t border-slate-200 pt-4 text-sm text-slate-600">
            <p className="font-medium text-slate-700">&iquest;Olvidaste tu contrase&ntilde;a?</p>
            <p className="mt-1">
              Escribe a <span className="font-medium text-ink">diego@tecnomania.com.mx</span> para soporte tecnico y
              restablecimiento de acceso.
            </p>
          </div>
        </div>
      </section>
    </div>
  );
}
