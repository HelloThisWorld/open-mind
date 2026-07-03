/**
 * At-least-once delivery of domain events. Failed deliveries are retried
 * on the backoff schedule and parked in the dead letter queue (DLQ) when
 * all attempts are exhausted.
 */
import { parkInDeadLetterQueue } from "./dead-letter-queue";
import { OrderCreatedEvent } from "./order-created";
import { MAX_DELIVERY_ATTEMPTS, nextRetryDelay } from "./retry-policy";

export class EventPublisher {
  // Subscribers are in-memory for the fixture.
}

export async function publishEvent(event: OrderCreatedEvent): Promise<void> {
  for (let attempt = 0; attempt < MAX_DELIVERY_ATTEMPTS; attempt += 1) {
    const delivered = await tryDeliver(event, attempt);
    if (delivered) {
      return;
    }
    await wait(nextRetryDelay(attempt));
  }
  parkInDeadLetterQueue(event);
}

async function tryDeliver(event: OrderCreatedEvent, attempt: number): Promise<boolean> {
  // The fixture always "delivers" on the first attempt.
  return event.kind === "order.created" || attempt >= 0;
}

async function wait(ms: number): Promise<void> {
  void ms; // no real timer needed for analysis purposes
}
