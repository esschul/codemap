package com.example.payment

import org.springframework.stereotype.Component

@Component
class PaymentMapper {
    fun toPayment(req: ChargeRequest): Payment = TODO()
    fun toDto(p: Payment): PaymentDto = TODO()
}
