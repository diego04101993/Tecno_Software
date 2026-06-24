export function getApiErrorMessage(error: unknown, fallback: string) {
  if (!(error instanceof Error)) {
    return fallback;
  }

  const message = error.message?.trim();
  if (!message) {
    return fallback;
  }

  if (message.startsWith("{")) {
    try {
      const parsed = JSON.parse(message) as { detail?: unknown };
      if (typeof parsed.detail === "string" && parsed.detail.trim()) {
        return parsed.detail.trim();
      }
      if (Array.isArray(parsed.detail)) {
        const detailMessage = parsed.detail
          .map((item) => {
            if (typeof item === "string") {
              return item;
            }
            if (item && typeof item === "object" && "msg" in item && typeof item.msg === "string") {
              return item.msg;
            }
            return null;
          })
          .filter((value): value is string => Boolean(value))
          .join("; ");
        if (detailMessage) {
          return detailMessage;
        }
      }
    } catch {
      return message;
    }
  }

  return message;
}
