export function parseServerDate(value) {
  if (!value) {
    return null;
  }
  const hasTimezone = /(?:Z|[+-]\d{2}:\d{2})$/.test(value);
  const normalized = hasTimezone ? value : `${value}Z`;
  const parsed = new Date(normalized);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }
  return parsed;
}

export function formatServerDateTime(value) {
  const parsed = parseServerDate(value);
  if (!parsed) {
    return "-";
  }
  return parsed.toLocaleString();
}

export function formatServerDate(value) {
  const parsed = parseServerDate(value);
  if (!parsed) {
    return "-";
  }
  return parsed.toLocaleDateString();
}

export function formatServerTime(value) {
  const parsed = parseServerDate(value);
  if (!parsed) {
    return "-";
  }
  return parsed.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
