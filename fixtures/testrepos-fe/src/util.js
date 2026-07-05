export function formatDate(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    return "unknown";
  }
  return d.toLocaleDateString();
}
