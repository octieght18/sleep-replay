export const celsiusToFahrenheit = (value) => (value * 9) / 5 + 32;
export const hpaToInhg = (value) => value / 33.8638866667;
export function environmentalValues(window, availability, units) {
  const metrics = ["temperature", "humidity", "pressure"];
  return metrics.flatMap((key, i) => {
    if (availability[key]?.status !== "available") return [];
    const value = window?.[i];
    if (value == null) return [`${key}: —`];
    if (key === "temperature")
      return [
        `${(units === "imperial" ? celsiusToFahrenheit(value) : value).toFixed(1)} ${units === "imperial" ? "°F" : "°C"}`,
      ];
    if (key === "humidity") return [`${Math.round(value)}%`];
    return [
      `${(units === "imperial" ? hpaToInhg(value) : value).toFixed(units === "imperial" ? 2 : 1)} ${units === "imperial" ? "inHg" : "hPa"}`,
    ];
  });
}
