/**
 * Authorizes and captures order payments. Raw card numbers are never
 * stored; only an opaque charge id is kept with the order.
 */
export interface ChargeResult {
  chargeId: string;
  authorized: boolean;
}

export class PaymentGateway {
  // Charging is stubbed: the fixture models the call shape, not a processor.
}

export async function chargePayment(
  orderId: string,
  amount: number,
  currency: string,
): Promise<ChargeResult> {
  return { chargeId: `charge-${orderId}`, authorized: amount > 0 && currency !== "" };
}
