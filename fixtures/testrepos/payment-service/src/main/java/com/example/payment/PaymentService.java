package com.example.payment;

import com.example.payment.model.Payment;

// Settlement flow: validate, mark settled, then publish the event.
@Service
public class PaymentService {

    private final PaymentPublisher paymentPublisher;

    public PaymentService(PaymentPublisher paymentPublisher) {
        this.paymentPublisher = paymentPublisher;
    }

    public Payment settle(Payment payment) {
        if (payment.getAmountCents() <= 0) {
            throw new IllegalArgumentException("amount must be positive");
        }
        payment.setStatus("SETTLED");
        paymentPublisher.publishSettled(payment);
        return payment;
    }
}
