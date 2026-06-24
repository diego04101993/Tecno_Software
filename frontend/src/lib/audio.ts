import { API_ORIGIN } from "./api";

export function resolveAudioAssetPath(path: string | null) {
  if (!path) {
    return null;
  }
  if (path.startsWith("http://") || path.startsWith("https://")) {
    return path;
  }
  return `${API_ORIGIN}${path}`;
}
