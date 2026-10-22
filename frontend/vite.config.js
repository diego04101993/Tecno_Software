import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
function assertProductionApi(rawUrl) {
    var _a;
    var value = rawUrl === null || rawUrl === void 0 ? void 0 : rawUrl.trim();
    try {
        if (!value || /[\s\u0000-\u001f\u007f]/.test(value)) {
            throw new Error();
        }
        var url = new URL(value);
        // URL.port normalizes an explicit :443 away; also check the raw authority.
        var address = value.match(/^https:\/\/([^/?#]+)(\/[^?#]*)?$/i);
        var authority = address === null || address === void 0 ? void 0 : address[1];
        if (url.origin !== "https://api.tecnocontrolcloud.com" ||
            (authority === null || authority === void 0 ? void 0 : authority.toLowerCase()) !== "api.tecnocontrolcloud.com" ||
            url.username || url.password || url.port ||
            value.indexOf("?") !== -1 || value.indexOf("#") !== -1 ||
            ["", "/", "/api", "/api/"].indexOf((_a = address === null || address === void 0 ? void 0 : address[2]) !== null && _a !== void 0 ? _a : "") === -1 ||
            ["/", "/api", "/api/"].indexOf(url.pathname) === -1) {
            throw new Error();
        }
    }
    catch (_b) {
        throw new Error("Invalid production API configuration: VITE_API_BASE_URL or VITE_API_URL must resolve to https://api.tecnocontrolcloud.com/api");
    }
}
export default defineConfig({
    plugins: [
        {
            name: "production-api-configuration",
            configResolved: function (_a) {
                var _b, _c;
                var command = _a.command, mode = _a.mode, env = _a.env;
                if (command !== "build" || mode !== "production")
                    return;
                assertProductionApi(((_b = env.VITE_API_BASE_URL) === null || _b === void 0 ? void 0 : _b.trim()) || ((_c = env.VITE_API_URL) === null || _c === void 0 ? void 0 : _c.trim()));
            },
        },
        react(),
    ],
    server: {
        host: "0.0.0.0",
        port: 5173,
    },
});
