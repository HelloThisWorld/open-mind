/**
 * The dead letter queue (DLQ) stores events whose delivery attempts are
 * exhausted so an operator can inspect and replay them.
 */
import { OrderCreatedEvent } from "./order-created";

export const DLQ_TOPIC = "orders.dead-letter";

const parked: OrderCreatedEvent[] = [];

export function parkInDeadLetterQueue(event: OrderCreatedEvent): void {
  parked.push(event);
}

export function listParkedEvents(): OrderCreatedEvent[] {
  return [...parked];
}
