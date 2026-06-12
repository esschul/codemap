package com.example.payment

import org.springframework.stereotype.Service

@Service
class PaymentService(
    private val stripeClient: StripeClient,
    private val paymentRepository: PaymentRepository,
    private val paymentMapper: PaymentMapper,
    private val orderService: com.example.order.OrderService,
) {
    fun charge(req: ChargeRequest): ChargeResult {
        val payment = paymentMapper.toPayment(req)
        val result = stripeClient.charge(payment)
        paymentRepository.save(result)
        return result
    }

    fun refund(id: String): RefundResult {
        val payment = paymentRepository.findById(id)
        return stripeClient.refund(payment)
    }

    fun findById(id: String): PaymentDto = paymentMapper.toDto(paymentRepository.findById(id))
}
