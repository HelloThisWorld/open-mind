/**
 * Order routes. POST /orders creates an order; the idempotency key header
 * makes retried submissions safe to replay.
 */
import { HttpApp } from "../http-app";
import { createOrder } from "../services/order-service";

export function registerOrderRoutes(app: HttpApp): void {
  app.post("/orders", async (body) => {
    return createOrder({
      orderId: String(body.orderId ?? ""),
      amount: Number(body.amount ?? 0),
      currency: String(body.currency ?? "USD"),
      idempotencyKey: String(body.idempotencyKey ?? ""),
    });
  });
}
