package com.example.payment.model;

public class Payment {

    private String id;
    private String orderId;
    private long amountCents;
    private String status;

    public String getId() { return id; }
    public void setId(String id) { this.id = id; }

    public String getOrderId() { return orderId; }
    public void setOrderId(String orderId) { this.orderId = orderId; }

    public long getAmountCents() { return amountCents; }
    public void setAmountCents(long amountCents) { this.amountCents = amountCents; }

    public String getStatus() { return status; }
    public void setStatus(String status) { this.status = status; }
}
