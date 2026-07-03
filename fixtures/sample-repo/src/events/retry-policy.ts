/**
 * Exponential backoff for failed event deliveries. Attempts wait 1s, 4s,
 * 16s, 64s, then 256s; after the last attempt the event is parked in the
 * dead letter queue (DLQ).
 */
export const RETRY_DELAYS_MS = [1_000, 4_000, 16_000, 64_000, 256_000];
export const MAX_DELIVERY_ATTEMPTS = 5;

export function nextRetryDelay(attempt: number): number {
  const index = Math.min(attempt, RETRY_DELAYS_MS.length - 1);
  return RETRY_DELAYS_MS[index] ?? 0;
}
