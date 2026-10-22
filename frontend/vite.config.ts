import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

function assertProductionApi(rawUrl: string | undefined) {
  const value = rawUrl?.trim();
  try {
    if (!value || /[\s\u0000-\u001f\u007f]/.test(value)) {
      throw new Error();
    }
    const url = new URL(value);
    // URL.port normalizes an explicit :443 away; also check the raw authority.
    const address = value.match(/^https:\/\/([^/?#]+)(\/[^?#]*)?$/i);
    const authority = address?.[1];
    if (
      url.origin !== "https://api.tecnocontrolcloud.com" ||
      authority?.toLowerCase() !== "api.tecnocontrolcloud.com" ||
      url.username || url.password || url.port ||
      value.indexOf("?") !== -1 || value.indexOf("#") !== -1 ||
      ["", "/", "/api", "/api/"].indexOf(address?.[2] ?? "") === -1 ||
      ["/", "/api", "/api/"].indexOf(url.pathname) === -1
    ) {
      throw new Error();
    }
  } catch {
    throw new Error(
      "Invalid production API configuration: VITE_API_BASE_URL or VITE_API_URL must resolve to https://api.tecnocontrolcloud.com/api",
    );
  }
}

export default defineConfig({
  plugins: [
    {
      name: "production-api-configuration",
      configResolved({ command, mode, env }) {
        if (command !== "build" || mode !== "production") return;
        assertProductionApi(env.VITE_API_BASE_URL?.trim() || env.VITE_API_URL?.trim());
      },
    },
    react(),
  ],
  server: {
    host: "0.0.0.0",
    port: 5173,
  },
});

