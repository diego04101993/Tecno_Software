import type { KioskButton, KioskScreen, TouchLocation, TouchRuntimeConfig } from "../types/domain";

export type TouchHotspotIntent = "screen" | "location" | "home" | "url";

export type TouchHotspotDraft = {
  label: string;
  x: number;
  y: number;
  width: number;
  height: number;
  sort_order: number;
  intent: TouchHotspotIntent;
  target_screen_id: string;
  location_id: string;
  action_value: string;
  backgroundColor: string;
  textColor: string;
  opacity: number;
};

export function slugifyTouchName(value: string) {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export function createDefaultHotspotDraft(): TouchHotspotDraft {
  return {
    label: "",
    x: 24,
    y: 24,
    width: 220,
    height: 72,
    sort_order: 1,
    intent: "screen",
    target_screen_id: "",
    location_id: "",
    action_value: "",
    backgroundColor: "#ffffff",
    textColor: "#0f172a",
    opacity: 0.8,
  };
}

export function parseHotspot(button: KioskButton): TouchHotspotDraft {
  const style = button.style_json ?? {};
  const payload = button.action_payload_json ?? {};
  let intent: TouchHotspotIntent = "screen";
  if (button.action_type === "open_url") {
    intent = "url";
  } else if (button.action_type === "navigate_menu") {
    if (payload.kind === "location") {
      intent = "location";
    } else if (payload.kind === "home") {
      intent = "home";
    }
  }

  return {
    label: button.label,
    x: button.x,
    y: button.y,
    width: button.width,
    height: button.height,
    sort_order: button.sort_order,
    intent,
    target_screen_id: button.target_screen_id ?? "",
    location_id: typeof payload.location_id === "string" ? payload.location_id : "",
    action_value: button.action_value ?? "",
    backgroundColor: typeof style.backgroundColor === "string" ? style.backgroundColor : "#ffffff",
    textColor: typeof style.textColor === "string" ? style.textColor : "#0f172a",
    opacity: typeof style.opacity === "number" ? style.opacity : 0.8,
  };
}

export function serializeHotspotDraft(draft: TouchHotspotDraft) {
  if (draft.intent === "screen") {
    return {
      action_type: "switch_screen",
      action_value: null,
      target_screen_id: draft.target_screen_id || null,
      action_payload_json: { kind: "screen" },
    };
  }

  if (draft.intent === "location") {
    return {
      action_type: "navigate_menu",
      action_value: null,
      target_screen_id: null,
      action_payload_json: { kind: "location", location_id: draft.location_id || null },
    };
  }

  if (draft.intent === "home") {
    return {
      action_type: "navigate_menu",
      action_value: null,
      target_screen_id: null,
      action_payload_json: { kind: "home" },
    };
  }

  return {
    action_type: "open_url",
    action_value: draft.action_value || null,
    target_screen_id: null,
    action_payload_json: { kind: "url" },
  };
}

export function getTouchHomeScreen(runtime: TouchRuntimeConfig) {
  return (
    runtime.screens.find((screen) => screen.id === runtime.experience.home_screen_id) ??
    runtime.screens.find((screen) => screen.screen_kind === "home") ??
    runtime.screens.find((screen) => !screen.is_attract_screen) ??
    runtime.screens[0] ??
    null
  );
}

export function getTouchAttractScreen(runtime: TouchRuntimeConfig) {
  return (
    runtime.screens.find((screen) => screen.id === runtime.experience.attract_screen_id) ??
    runtime.screens.find((screen) => screen.is_attract_screen || screen.screen_kind === "attract") ??
    runtime.screens[0] ??
    null
  );
}

export function resolveTouchTimeout(runtime: TouchRuntimeConfig, screen: KioskScreen | null) {
  return screen?.idle_timeout_override ?? runtime.experience.default_idle_timeout_seconds ?? screen?.inactivity_timeout_seconds ?? 30;
}

export function resolveTouchAction({
  button,
  runtime,
  currentScreen,
}: {
  button: KioskButton;
  runtime: TouchRuntimeConfig;
  currentScreen: KioskScreen | null;
}): { nextScreenId?: string | null; location?: TouchLocation | null; note?: string | null } {
  if (button.action_type === "switch_screen") {
    return { nextScreenId: button.target_screen_id, location: null };
  }

  if (button.action_type === "open_url") {
    return { nextScreenId: currentScreen?.id ?? null, location: null, note: button.action_value ?? "Abriria una URL externa." };
  }

  const payload = button.action_payload_json ?? {};
  if (payload.kind === "home") {
    return { nextScreenId: getTouchHomeScreen(runtime)?.id ?? getTouchAttractScreen(runtime)?.id ?? null, location: null };
  }
  if (payload.kind === "location") {
    const location = runtime.locations.find((item) => item.id === payload.location_id) ?? null;
    return { nextScreenId: currentScreen?.id ?? null, location };
  }

  return { nextScreenId: currentScreen?.id ?? null, location: null };
}
