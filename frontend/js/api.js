export class ApiError extends Error {
  constructor(payload) {
    super(payload.description || "The request failed.");
    Object.assign(this, payload);
  }
}
export async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new ApiError({
      code: "SOURCE_LOAD_FAILED",
      description: "The backend is not reachable.",
      action: "Start it with python -m backend.api.run.",
    });
  }
  let body;
  try {
    body = await response.json();
  } catch {
    throw new ApiError({
      code: "SOURCE_LOAD_FAILED",
      description: "The backend returned an unreadable response.",
      action: "Restart it with python -m backend.api.run and retry.",
    });
  }
  if (!response.ok) throw new ApiError(body);
  return body;
}
export const jsonRequest = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
export function showError(element, error) {
  element.textContent = [
    error.description || error.message,
    error.action || "Retry the operation.",
    error.file_name,
    error.reference_id ? `Reference: ${error.reference_id}` : "",
  ]
    .filter(Boolean)
    .join("\n");
  element.hidden = false;
}
